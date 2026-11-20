--[[--
T3 Code client for KOReader.

Shows T3 Code threads from a t3-koreader bridge and lets you reply with a tap.
All T3-specific work happens on the bridge; this plugin renders what it gets.

@module koplugin.t3
--]]

local ButtonDialog = require("ui/widget/buttondialog")
local Device = require("device")
local ImageViewer = require("ui/widget/imageviewer")
local lfs = require("libs/libkoreader-lfs")
local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local Dispatcher = require("dispatcher")
local Event = require("ui/event")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local logger = require("logger")
local socket = require("socket")
local _ = require("gettext")
local T = require("ffi/util").template

local Api = require("t3_api")
local Gestures = require("t3_gestures")
local ThreadView = require("t3_threadview")

local LIST_LIMIT = 8
local POLL_ACTIVE = 15 -- seconds, while a thread is working or waiting for you
local POLL_PROGRESS = 10 -- seconds, while working with "Show progress while working" on
local POLL_IDLE = 60 -- seconds, otherwise
local POLL_LIST = 30

local STATE_LABEL = {
    needs_you = _("needs you"),
    working = _("working"),
    failed = _("failed"),
    done = _("done"),
    idle = _("new"),
}

local T3 = WidgetContainer:extend{
    name = "t3",
    is_doc_only = false,
}

-- ---------------------------------------------------------------------------
-- setup

function T3:init()
    self.settings = LuaSettings:open(DataStorage:getSettingsDir() .. "/t3.lua")
    self:makeApi()
    Dispatcher:registerAction("t3_open", {
        category = "none", event = "T3Open", title = _("T3 Code threads"), general = true,
    })
    Dispatcher:registerAction("t3_reload", {
        category = "none", event = "T3Reload", title = _("Reload T3 plugin"), general = true,
    })
    self.ui.menu:registerToMainMenu(self)
end

function T3:makeApi()
    self.api = Api.new(self.settings:readSetting("bridge_url"), self.settings:readSetting("bridge_token"))
    bridge_api = self.api
end

-- Put "T3 Code" first in the Tools tab of both the file browser and the reader.
-- The menu order tables are shared modules that plugins may extend (KOReader's own
-- ui/plugin/insert_menu does the same for "More tools"). Done once per process.
local function placeFirstInTools()
    for _i, name in ipairs({ "ui/elements/filemanager_menu_order", "ui/elements/reader_menu_order" }) do
        local ok, order = pcall(require, name)
        if ok and type(order) == "table" and type(order.tools) == "table" then
            local present = false
            for _j, id in ipairs(order.tools) do
                if id == "t3" then present = true end
            end
            if not present then table.insert(order.tools, 1, "t3") end
        end
    end
end

function T3:addToMainMenu(menu_items)
    placeFirstInTools()
    -- One tap from the Tools tab opens the thread list.
    menu_items.t3 = {
        text = _("T3 Code"),
        callback = function() self:onT3Open() end,
    }
end

function T3:showProgress()
    return self.settings:nilOrTrue("show_progress")
end

--- Start each of your messages at the top of a page (on unless turned off).
function T3:pageBreakYou()
    return self.settings:nilOrTrue("page_break_you")
end

function T3:progressParam()
    return self:showProgress() and 1 or nil
end

--- Finished turns you've opened, per thread: { [thread id] = { [run id] = true } }.
--- Kept in memory only; a fresh KOReader start shows every finished turn folded.
function T3:expandedRuns(id)
    self.expanded = self.expanded or {}
    self.expanded[id] = self.expanded[id] or {}
    return self.expanded[id]
end

function T3:expandList(id)
    local list = {}
    for run in pairs(self:expandedRuns(id)) do table.insert(list, run) end
    table.sort(list)
    return list
end

function T3:expandParam(id)
    local list = self:expandList(id)
    return #list > 0 and table.concat(list, ",") or nil
end

function T3:toggleRun(view, run)
    local runs = self:expandedRuns(view.id)
    runs[run] = not runs[run] or nil
    -- Stay on the page with the tapped row; its steps open right below it.
    self:reloadThread(view.id, self.viewer and self.viewer:currentPage() or nil)
end

--- The plugin's settings, from the menu button in the thread list. Each row shows its
--- current state; changing one reopens the dialog so the label is current.
function T3:showSettings()
    local dialog
    local function reopen()
        UIManager:close(dialog)
        self:showSettings()
    end
    local url = self.settings:readSetting("bridge_url")
    local token = self.settings:readSetting("bridge_token")
    dialog = ButtonDialog:new{
        title = _("T3 Code settings"),
        buttons = {
            {{
                text = self:showProgress() and _("Show progress while working: on")
                    or _("Show progress while working: off"),
                callback = function()
                    self.settings:saveSetting("show_progress", not self:showProgress())
                    self.settings:flush()
                    reopen()
                end,
            }},
            {{
                text = self:pageBreakYou() and _("Your messages start a new page: on")
                    or _("Your messages start a new page: off"),
                callback = function()
                    self.settings:saveSetting("page_break_you", not self:pageBreakYou())
                    self.settings:flush()
                    reopen()
                end,
            }},
            {{
                text = T(_("Bridge address: %1"), (url and url ~= "") and url or _("not set")),
                callback = function()
                    UIManager:close(dialog)
                    self:editSetting("bridge_url", _("Bridge address"), "http://100.64.0.1:8787", false)
                end,
            }},
            {{
                text = T(_("Bridge token: %1"), (token and token ~= "") and _("set") or _("not set")),
                callback = function()
                    UIManager:close(dialog)
                    self:editSetting("bridge_token", _("Bridge token"), "", true)
                end,
            }},
            {{ text = _("Close"), callback = function() UIManager:close(dialog) end }},
        },
    }
    UIManager:show(dialog)
end

function T3:editSetting(key, title, hint, secret)
    local dialog
    dialog = InputDialog:new{
        title = title,
        input = self.settings:readSetting(key) or "",
        input_hint = hint,
        text_type = secret and "password" or nil,
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            {
                text = _("Save"),
                is_enter_default = true,
                callback = function()
                    local value = dialog:getInputText():gsub("^%s+", ""):gsub("%s+$", "")
                    self.settings:saveSetting(key, value)
                    self.settings:flush()
                    self:makeApi()
                    UIManager:close(dialog)
                end,
            },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

-- ---------------------------------------------------------------------------
-- helpers

local function notify(text, timeout)
    UIManager:show(InfoMessage:new{ text = text, timeout = timeout or 4 })
end

-- Staying connected. KOReader turns Wi-Fi off before the Kobo sleeps and, unless its
-- "Restore Wi-Fi connection on resume" is on, leaves it off on waking. So a request
-- turns Wi-Fi back on first, and waking up with T3 open reconnects (T3:onResume).

local bridge_api -- the plugin's Api (set by T3:makeApi), to check the bridge answers

local function offline()
    return Device:hasWifiToggle() and not NetworkMgr:isConnected()
end

--- Blocking, like the requests themselves: turn Wi-Fi on and wait up to 30 s for an
--- address, then up to 20 s for the bridge (over Tailscale, the tunnel takes a few
--- seconds more). Returns true once connected.
local function ensureConnected()
    if not offline() then return true end
    local msg = InfoMessage:new{ text = _("Wi-Fi is off. Reconnecting…"), dismissable = false }
    UIManager:show(msg)
    UIManager:forceRePaint()
    if not NetworkMgr.pending_connection then
        local ok, err = pcall(NetworkMgr.enableWifi, NetworkMgr, nil, false)
        if not ok then logger.warn("t3: enabling Wi-Fi failed", err) end
    end
    local deadline = os.time() + 30
    while not NetworkMgr:isConnected() and os.time() < deadline do socket.sleep(0.25) end
    local connected = NetworkMgr:isConnected()
    if connected and bridge_api then
        deadline = os.time() + 20
        while not bridge_api:health() and os.time() < deadline do socket.sleep(1) end
    end
    UIManager:close(msg)
    logger.info("t3: reconnected:", connected)
    return connected
end

--- Run fn while showing a short "working" message, so a slow call doesn't look like a freeze.
--- Every call to the bridge goes through here, so this is also where Wi-Fi comes back on.
local function withBusy(text, fn)
    if not ensureConnected() then
        return false, _("Couldn't connect to Wi-Fi. Check it's on and in range, then try again.")
    end
    local msg = InfoMessage:new{ text = text, dismissable = false }
    UIManager:show(msg)
    UIManager:forceRePaint()
    local ok, a, b, c = pcall(fn)
    UIManager:close(msg)
    if not ok then
        logger.err("t3:", a)
        return false, tostring(a)
    end
    return a, b, c
end

local utc_offset
local function parseIso(s)
    if type(s) ~= "string" then return nil end
    local y, mo, d, h, mi, sec = s:match("^(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)")
    if not y then return nil end
    if not utc_offset then
        local now = os.time()
        utc_offset = os.difftime(now, os.time(os.date("!*t", now)))
    end
    return os.time{ year = y, month = mo, day = d, hour = h, min = mi, sec = sec } + utc_offset
end

local function ago(iso)
    local t = parseIso(iso)
    if not t then return "" end
    local s = os.difftime(os.time(), t)
    if s < 60 then return _("now") end
    if s < 3600 then return T(_("%1m"), math.floor(s / 60)) end
    if s < 86400 then return T(_("%1h"), math.floor(s / 3600)) end
    return T(_("%1d"), math.floor(s / 86400))
end

--- When a snoozed thread wakes, in the device's time: "18:00" today, else "Sat 9:00".
local function wakeLabel(iso)
    local t = parseIso(iso)
    if not t then return "" end
    local clock = os.date("%H:%M", t):gsub("^0", "")
    if os.date("%Y%m%d", t) == os.date("%Y%m%d") then return clock end
    return os.date("%a ", t) .. clock
end

local function str(v)
    return type(v) == "string" and v or nil
end

-- ---------------------------------------------------------------------------
-- thread list

function T3:onT3Open()
    self:showList() -- turns Wi-Fi on if it's off (withBusy)
    return true
end

--- Woken up with T3 open: if KOReader isn't bringing Wi-Fi back itself, turn it on.
--- The open thread or list then refreshes (onNetworkConnected).
function T3:onResume()
    if not (self.viewer or self.list) then return end
    -- A moment first, for KOReader's own restore (its setting) to start.
    UIManager:scheduleIn(2, function()
        if not (self.viewer or self.list) or not offline() then return end
        if NetworkMgr:isWifiOn() or NetworkMgr.pending_connection then return end
        logger.info("t3: woke with Wi-Fi off, reconnecting")
        NetworkMgr:turnOnWifiAndWaitForConnection(function() end)
    end)
end

--- Wi-Fi is back (from anywhere): check the open thread or list for changes now
--- rather than at the next poll. A couple of seconds first, for Tailscale.
function T3:onNetworkConnected()
    local poll = self.viewer and self.thread_poll or (not self.viewer and self.list_poll)
    if poll then
        UIManager:unschedule(poll)
        UIManager:scheduleIn(3, poll)
    end
end

function T3:listItems(body, project_filter)
    local items = {}
    for _i, p in ipairs(body.projects or {}) do
        local header = str(p.title) or _("No project")
        if not project_filter then
            -- What tapping the header reveals: more active threads, and the settled and
            -- snoozed ones.
            local extra = {}
            local more = tonumber(p.more) or 0
            local settled = tonumber(p.settled) or 0
            local snoozed = tonumber(p.snoozed) or 0
            if more > 0 then table.insert(extra, T(_("%1 more"), more)) end
            if snoozed > 0 then table.insert(extra, T(_("%1 snoozed"), snoozed)) end
            if settled > 0 then table.insert(extra, T(_("%1 settled"), settled)) end
            if #extra > 0 then header = header .. "  (" .. table.concat(extra, " · ") .. ")" end
        end
        table.insert(items, {
            text = header,
            bold = true,
            callback = function()
                if not project_filter and str(p.id) then self:showList(p.id, str(p.title)) end
            end,
            -- Hold a project's name to start a thread in it (not the Pinned group).
            hold_callback = str(p.id) and p.pinned ~= true and function() self:projectActions(p) end or nil,
        })
        for _j, t in ipairs(p.threads or {}) do
            local state = str(t.state) or "done"
            local label = STATE_LABEL[state] or state
            if state == "done" then
                label = (t.unread == true and _("new") .. " · " or "") .. ago(t.updated)
            end
            -- Subagents live inside their parent; say so when one needs attention.
            local agents = type(t.agents) == "table" and t.agents or nil
            local agent_needs_you = agents and (tonumber(agents.needs_you) or 0) > 0
            if agent_needs_you and state ~= "needs_you" then
                label = _("agent needs you")
            elseif agents and (tonumber(agents.working) or 0) > 0 and state ~= "working"
                    and state ~= "needs_you" then
                label = _("agents working")
            end
            if t.settled == true and state ~= "needs_you" and state ~= "working" then
                label = _("settled") .. " · " .. ago(t.updated)
            end
            if str(t.snoozed_until) then
                label = T(_("until %1"), wakeLabel(t.snoozed_until))
            end
            table.insert(items, {
                text = "    " .. (str(t.title) or _("Untitled")),
                mandatory = label,
                bold = state == "needs_you" or agent_needs_you or t.unread == true,
                dim = t.settled == true and state ~= "needs_you" and state ~= "working" or nil,
                callback = function() self:openThread(t.id) end,
                hold_callback = function() self:threadActions(t) end,
            })
        end
    end
    if #items == 0 then
        table.insert(items, { text = _("No threads yet.") })
    end
    -- A problem with the bridge itself (e.g. its T3 token is about to expire) goes on top.
    if str(body.notice) then
        table.insert(items, 1, { text = "! " .. str(body.notice), bold = true })
    end
    return items
end

function T3:showList(project_id, title)
    self.thread_stack = {}
    local params = { limit = project_id and 200 or LIST_LIMIT, project = project_id }
    local ok, body = withBusy(_("Loading threads…"), function()
        return self.api:request("GET", "/v1/threads", params)
    end)
    if not ok then
        -- With no list there's no menu button, so offer the settings here; a wrong
        -- bridge address or token is the usual cause.
        UIManager:show(ConfirmBox:new{
            text = body,
            ok_text = _("Settings"),
            ok_callback = function() self:showSettings() end,
        })
        return
    end
    local menu
    menu = Menu:new{
        title = title or _("T3 Code"),
        item_table = self:listItems(body, project_id),
        is_popout = false,
        is_borderless = true,
        covers_fullscreen = true,
        title_bar_left_icon = "appbar.menu",
        close_callback = function()
            UIManager:close(menu)
            if self.list and self.list.menu == menu then
                self.list = self.list.parent
                -- Back from a project's list to the full one.
                self:refreshListOnReturn()
            end
        end,
    }
    -- Keep the list open underneath a thread instead of closing on tap.
    menu.onMenuSelect = function(_menu, item)
        if item.callback then item.callback() end
        return true
    end
    menu.onMenuHold = function(_menu, item)
        if item.hold_callback then item.hold_callback() end
        return true
    end
    menu.onLeftButtonTap = function() self:showListActions() end
    Gestures.attach(menu)
    self.list = { menu = menu, v = body.v, params = params, project_id = project_id, title = title,
        parent = self.list }
    UIManager:show(menu)
    self:scheduleListPoll()
end

function T3:refreshList(force)
    local list = self.list
    if not list then return end
    local ok, body
    if force then
        ok, body = self.api:request("GET", "/v1/threads", list.params)
    else
        ok, body = self.api:listChangedSince(list.v, list.params.limit)
        if ok and body.changed ~= true then return end
    end
    if not ok or self.list ~= list then return end
    list.v = body.v
    local menu = list.menu
    local first = ((menu.page or 1) - 1) * (menu.perpage or 1) + 1
    menu:switchItemTable(nil, self:listItems(body, list.project_id), first)
end

--- Coming back to the list: reload it in full. A changed-since check isn't enough here:
--- it can miss the thread just read losing its "new", and the "ago" times would be stale.
function T3:refreshListOnReturn()
    if not self.list or not NetworkMgr:isConnected() then return end
    pcall(function() self:refreshList(true) end)
    -- Restart the poll so it doesn't fire right after this.
    self:scheduleListPoll()
end

function T3:scheduleListPoll()
    if self.list_poll then UIManager:unschedule(self.list_poll) end
    self.list_poll = function()
        self.list_poll = nil
        if not self.list then return end
        -- Don't redraw the list while a thread is open on top of it.
        if not self.viewer and not self.typing and NetworkMgr:isConnected() then
            pcall(function() self:refreshList(false) end)
        end
        self:scheduleListPoll()
    end
    UIManager:scheduleIn(POLL_LIST, self.list_poll)
end

--- Run a thread action, then `done(kind)` shows the result (by default: the list as
--- it now is).
function T3:listAction(busy_text, fn, done, kind)
    local ok, err = withBusy(busy_text, fn)
    if not ok then
        notify(err)
        return
    end
    if done then return done(kind) end
    pcall(function() self:refreshList(true) end)
end

--- Hold the title of an open thread: the same menu as holding it in the list. After an
--- action the thread closes, back to the list (which refreshes on the way).
function T3:viewActions(view)
    self:threadActions(view, function()
        if not self.view or self.view.id ~= view.id then return end -- closed meanwhile
        if self.viewer then self.viewer:onClose() end
    end, { text = _("Reload T3 plugin"), callback = function() self:reloadPlugin() end })
end

--- The app's snooze choices. This evening only while it's more than an hour away.
function T3:snoozeThread(t, done)
    local presets = {
        { "hour", _("In 1 hour") },
        { "three-hours", _("In 3 hours") },
    }
    local now = os.date("*t")
    if now.hour < 17 then table.insert(presets, { "evening", _("This evening") }) end
    table.insert(presets, { "tomorrow", _("Tomorrow morning") })
    if now.wday ~= 1 then -- on a Sunday, tomorrow is already next week's Monday
        table.insert(presets, { "next-week", _("Next week") })
    end
    local dialog
    local rows = {}
    for _i, p in ipairs(presets) do
        table.insert(rows, {{
            text = p[2],
            callback = function()
                UIManager:close(dialog)
                self:listAction(_("Snoozing…"), function()
                    return self.api:threadAction(t.id, "snooze", p[1])
                end, done, "snooze")
            end,
        }})
    end
    dialog = ButtonDialog:new{ title = _("Snooze until…"), buttons = rows }
    UIManager:show(dialog)
end

--- Hold on a project's name in the list: start a thread in it.
function T3:projectActions(p)
    local dialog
    dialog = ButtonDialog:new{
        title = str(p.title) or _("Project"),
        buttons = {
            {{ text = _("New thread"), callback = function()
                UIManager:close(dialog)
                self:newThread({ id = p.id, title = str(p.title) })
            end }},
            {{ text = _("Cancel"), callback = function() UIManager:close(dialog) end }},
        },
    }
    UIManager:show(dialog)
end

--- Rename a thread: a one-line box with its title, or Auto for a title T3 writes.
function T3:renameThread(t, done)
    self:compose{
        title = _("Rename thread"),
        input = str(t.title) or "",
        hint = _("Title"),
        one_line = true,
        send_text = _("Save"),
        extras = { {
            text = _("Auto"),
            callback = function()
                if self.compose_dialog then UIManager:close(self.compose_dialog) end
                self:listAction(_("Asking T3 for a title…"), function()
                    return self.api:threadAction(t.id, "auto_title")
                end, done, "rename")
            end,
        } },
        on_send = function(text)
            self:listAction(_("Renaming…"), function()
                return self.api:threadAction(t.id, "rename", nil, text)
            end, done, "rename")
        end,
    }
end

--- Hold on a thread in the list (or the title of an open one): the app's thread menu
--- (pin, snooze, mark unread, settle, archive). `done(kind)`: what to show afterwards.
--- `extra`: one more button ({text, callback}), above Cancel.
function T3:threadActions(t, done, extra)
    local settled = t.settled == true
    local pinned = t.pinned == true
    local snoozed = str(t.snoozed_until) ~= nil
    local state = str(t.state) or "done"
    local busy = state == "working" or state == "needs_you"
    local dialog
    local function action(text, busy_text, fn)
        return {{
            text = text,
            callback = function()
                UIManager:close(dialog)
                self:listAction(busy_text, fn, done)
            end,
        }}
    end
    local rows = {}
    table.insert(rows, action(pinned and _("Unpin") or _("Pin"), pinned and _("Unpinning…") or _("Pinning…"),
        function() return self.api:threadAction(t.id, pinned and "unpin" or "pin") end))
    table.insert(rows, {{
        text = _("Rename…"),
        callback = function()
            UIManager:close(dialog)
            self:renameThread(t, done)
        end,
    }})
    if snoozed then
        table.insert(rows, action(_("Wake now"), _("Waking…"),
            function() return self.api:threadAction(t.id, "unsnooze") end))
    elseif state ~= "needs_you" then
        table.insert(rows, {{
            text = _("Snooze…"),
            callback = function()
                UIManager:close(dialog)
                self:snoozeThread(t, done)
            end,
        }})
    end
    if t.can_mark_unread == true then
        table.insert(rows, action(_("Mark unread"), _("Marking unread…"),
            function() return self.api:threadAction(t.id, "mark_unread") end))
    end
    table.insert(rows, action(settled and _("Unsettle") or _("Settle"),
        settled and _("Unsettling…") or _("Settling…"),
        function() return self.api:settle(t.id, not settled) end))
    if not busy then
        table.insert(rows, {{
            text = _("Archive"),
            callback = function()
                UIManager:close(dialog)
                UIManager:show(ConfirmBox:new{
                    text = T(_("Archive \"%1\"?\nIt leaves the list; unarchive it in T3 on the desktop."),
                        str(t.title) or _("this thread")),
                    ok_text = _("Archive"),
                    ok_callback = function()
                        self:listAction(_("Archiving…"), function()
                            return self.api:threadAction(t.id, "archive")
                        end, done, "archive")
                    end,
                })
            end,
        }})
    end
    if extra then
        table.insert(rows, {{
            text = extra.text,
            callback = function()
                UIManager:close(dialog)
                extra.callback()
            end,
        }})
    end
    table.insert(rows, {{ text = _("Cancel"), callback = function() UIManager:close(dialog) end }})
    dialog = ButtonDialog:new{ title = str(t.title) or _("Thread"), buttons = rows }
    UIManager:show(dialog)
end

function T3:showListActions()
    local dialog
    dialog = ButtonDialog:new{
        buttons = {
            {{ text = _("New thread"), callback = function()
                UIManager:close(dialog)
                self:chooseProject()
            end }},
            {{ text = _("Refresh"), callback = function()
                UIManager:close(dialog)
                withBusy(_("Refreshing…"), function() self:refreshList(true) end)
            end }},
            {{ text = _("Settings"), callback = function()
                UIManager:close(dialog)
                self:showSettings()
            end }},
            {{ text = _("Reload T3 plugin"), callback = function()
                UIManager:close(dialog)
                self:reloadPlugin()
            end }},
        },
    }
    UIManager:show(dialog)
end

-- ---------------------------------------------------------------------------
-- reloading

local MODULES = { "t3_api", "t3_gestures", "t3_threadview" }

function T3:onT3Reload()
    self:reloadPlugin()
    return true
end

--- Load the plugin's code again (after new files were copied over) without restarting
--- KOReader, then reopen the list and thread that were open. The new code replaces the
--- methods on the plugin's class, so this instance and later ones (a new file browser
--- or reader) both use it. If the new code doesn't load, the old code keeps running.
function T3:reloadPlugin()
    local lists = {} -- the open lists, the full one first (a project's list opens on top)
    local list = self.list
    while list do
        table.insert(lists, 1, { project_id = list.project_id, title = list.title })
        list = list.parent
    end
    local reopen_thread = self.view and self.view.id
    local page = self.viewer and self.viewer:currentPage()

    self:stopThreadPoll()
    if self.list_poll then
        UIManager:unschedule(self.list_poll)
        self.list_poll = nil
    end
    -- UIManager:close doesn't run the widgets' close callbacks, so nothing reopens.
    local viewer = self.viewer
    self.viewer, self.view, self.thread_stack = nil, nil, {}
    if viewer then UIManager:close(viewer) end
    while self.list do
        local menu = self.list.menu
        self.list = self.list.parent
        UIManager:close(menu)
    end

    local saved = {}
    for _i, name in ipairs(MODULES) do
        saved[name] = package.loaded[name]
        package.loaded[name] = nil
    end
    local ok, new = pcall(dofile, self.path .. "/main.lua")
    if ok and type(new) == "table" then
        local class = getmetatable(self)
        for k, v in pairs(new) do
            if k ~= "__index" then class[k] = v end
        end
        self:makeApi() -- with the new Api module
        logger.info("t3: plugin reloaded")
    else
        for name, mod in pairs(saved) do package.loaded[name] = mod end
        logger.err("t3: reload failed", new)
    end

    UIManager:nextTick(function()
        for _i, l in ipairs(lists) do self:showList(l.project_id, l.title) end
        if reopen_thread then self:openThread(reopen_thread, page) end
        if ok and type(new) == "table" then
            notify(_("T3 plugin reloaded."), 2)
        else
            notify(T(_("Couldn't reload the T3 plugin; still running the old code.\n%1"), tostring(new)), 10)
        end
    end)
end

-- ---------------------------------------------------------------------------
-- thread view

function T3:openThread(id, page)
    local ok, view = withBusy(_("Loading…"), function()
        return self.api:thread(id, { visit = 1, progress = self:progressParam(), expand = self:expandParam(id) })
    end)
    if not ok then
        notify(view)
        return
    end
    self:showThread(view, page and { page = page } or nil)
end

-- ---------------------------------------------------------------------------
-- images

local function imageDir()
    local dir = DataStorage:getDataDir() .. "/cache/t3-images"
    if not lfs.attributes(dir, "mode") then
        lfs.mkdir(DataStorage:getDataDir() .. "/cache")
        lfs.mkdir(dir)
    end
    return dir
end

-- KOReader's image widgets go by file extension, so cached images keep one.
local IMAGE_EXTS = { "png", "jpg", "gif", "webp" }
local EXT_FOR_TYPE = { ["image/png"] = "png", ["image/jpeg"] = "jpg", ["image/gif"] = "gif",
                       ["image/webp"] = "webp" }

local function cachedImage(dir, id)
    for _i, ext in ipairs(IMAGE_EXTS) do
        local name = id .. "." .. ext
        if lfs.attributes(dir .. "/" .. name, "mode") == "file" then return name end
    end
end

--- Download the view's images that aren't cached yet (sized to the screen by the bridge).
--- Returns { [id] = file name in imageDir() } for the ones available.
function T3:fetchImages(view)
    local have = {}
    if type(view.images) ~= "table" or #view.images == 0 then return have end
    local dir = imageDir()
    self.image_failed = self.image_failed or {}
    local missing = {}
    for _i, id in ipairs(view.images) do
        id = str(id)
        if id and id:match("^%w+$") then
            local name = cachedImage(dir, id)
            if name then
                have[id] = name
            elseif not self.image_failed[id] then
                table.insert(missing, id)
            end
        end
    end
    if #missing == 0 then return have end
    withBusy(_("Loading images…"), function()
        for _i, id in ipairs(missing) do
            have[id] = self:downloadImage(id)
        end
    end)
    return have
end

--- Fetch one image into the cache; returns its file name, or nil.
function T3:downloadImage(id)
    local dir = imageDir()
    local tmp = dir .. "/" .. id .. ".download"
    local ok, ctype = self.api:download("/v1/images/" .. id, { w = Device.screen:getWidth() }, tmp)
    local ext = ok and EXT_FOR_TYPE[(tostring(ctype or "")):match("^[^;%s]+")]
    if not ext then
        os.remove(tmp)
        logger.warn("t3: image", id, ok and ("unsupported type " .. tostring(ctype)) or ctype)
        self.image_failed = self.image_failed or {}
        self.image_failed[id] = true -- don't retry on every redraw
        return nil
    end
    os.rename(tmp, dir .. "/" .. id .. "." .. ext)
    return id .. "." .. ext
end

--- Full-screen view of an image; screenshots in tool steps are fetched on first tap.
function T3:showImage(id)
    local dir = imageDir()
    local name = cachedImage(dir, id)
    if not name then
        local _ok, fetched = withBusy(_("Loading image…"), function() return true, self:downloadImage(id) end)
        name = fetched
    end
    if not name then
        notify(_("Couldn't load this image."))
        return
    end
    UIManager:show(ImageViewer:new{ file = dir .. "/" .. name, fullscreen = true, with_title_bar = false })
end

local function esc(text)
    return (tostring(text or ""):gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;"))
end

local ROLE_LABEL = { you = _("You"), agent = _("Agent") }

--- The thread as HTML. Entries arrive pre-rendered from the bridge (`html`);
--- `text` is the plain fallback for older bridges.
--- Swap the bridge's image references for downloaded copies, or a placeholder.
local function placeImages(html, have)
    return (html:gsub('<a href="t3:img:(%w+)"><img src="t3img:%1" alt="([^"]*)"/></a>', function(id, alt)
        if have[id] then
            return '<a href="t3:img:' .. id .. '"><img src="' .. have[id] .. '" alt="' .. alt .. '"/></a>'
        end
        return "<i>[" .. (alt ~= "" and alt or "image") .. "]</i>"
    end))
end

local ANCHOR = "t3:anchor"

--- The entry to open the thread at: the agent's latest reply when it's the last
--- thing in the thread, so a long reply is read from its start. With `old` (the view
--- being replaced), only when that reply is new or the turn has just ended.
local function anchorIndex(view, old)
    local entries = view.entries or {}
    local last = entries[#entries]
    if not last or last.role ~= "agent" then return nil end
    if old and old.state == "working" and view.state ~= "working" then return #entries end
    if old then
        local seen = old.entries and old.entries[#old.entries]
        if seen and type(seen.o) == "number" and type(last.o) == "number" and last.o <= seen.o then
            return nil
        end
    end
    return #entries
end

local function composeHtml(view, have, anchor, page_break_you)
    local parts = {}
    if view.more_before == true then
        table.insert(parts, '<div class="more"><a href="t3:older">' .. esc(_("⋯ Show earlier messages")) .. "</a></div>")
    end
    for i, e in ipairs(view.entries or {}) do
        local role = str(e.role) or "note"
        local body = str(e.html) or ("<p>" .. esc(str(e.text)) .. "</p>")
        body = placeImages(body, have or {})
        local label = ROLE_LABEL[role]
        if role == "fold" then label = nil end
        label = label and esc(label)
        if label and i == anchor then label = '<a href="' .. ANCHOR .. '">' .. label .. "</a>" end
        -- Optionally each of your messages starts a new page, so a turn reads from the top.
        local newpage = (page_break_you and role == "you" and i > 1) and " newpage" or ""
        table.insert(parts, '<div class="entry ' .. role .. newpage .. '">'
            .. (label and ('<div class="label">' .. label .. "</div>") or "")
            .. body .. "</div>")
    end
    local state = str(view.state)
    if state == "working" then
        table.insert(parts, '<div class="status">' .. esc(_("Working…")) .. "</div>")
    elseif state == "failed" then
        table.insert(parts, '<div class="status">' .. esc(_("The last run failed.")) .. "</div>")
    end
    -- Linked PRs, as T3 last synced them: "PR #12 open · checks pass · needs review".
    -- (T3 checks open PRs every few minutes, so there's no "as of" to show.)
    local prs = {}
    for _i, pr in ipairs(type(view.prs) == "table" and view.prs or {}) do
        local number = type(pr.number) == "number" and tostring(pr.number) or str(pr.number) or "?"
        local line = "<b>#" .. esc(number) .. "</b> " .. esc(str(pr.status) or "")
        if str(pr.title) then line = line .. " · <i>" .. esc(str(pr.title)) .. "</i>" end
        table.insert(prs, "<p>" .. line .. "</p>")
    end
    if #prs > 0 then
        local heading = #prs > 1 and _("Pull requests") or _("Pull request")
        table.insert(parts, '<div class="prs"><div class="pending-title">' .. esc(heading)
            .. "</div>" .. table.concat(prs) .. "</div>")
    end
    if str(view.mode) == "plan" then
        table.insert(parts, '<div class="status">' .. esc(_("Plan mode is on.")) .. "</div>")
    end
    local p = view.pending
    if type(p) == "table" then
        local lines = { '<div class="pending"><div class="pending-title">' .. esc(_("Needs you")) .. "</div>" }
        local head = str(p.header)
        local step = str(p.step)
        table.insert(lines, "<p>" .. (head and ("<b>" .. esc(head) .. ":</b> ") or "") .. esc(str(p.text))
            .. (step and (" (" .. esc(step) .. ")") or "") .. "</p>")
        if str(p.link) then
            table.insert(lines, "<p>" .. esc(_("Page:")) .. " " .. esc(str(p.link)) .. "</p>")
        end
        if str(p.note) then table.insert(lines, "<p><i>" .. esc(str(p.note)) .. "</i></p>") end
        local opts = {}
        for _i, o in ipairs(type(p.options) == "table" and p.options or {}) do
            local desc = str(o.description)
            if desc == str(o.label) then desc = nil end -- "Apple: Apple" says nothing
            table.insert(opts, "<li><b>" .. esc(str(o.label)) .. "</b>" .. (desc and (": " .. esc(desc)) or "") .. "</li>")
        end
        if #opts > 0 then table.insert(lines, "<ul>" .. table.concat(opts) .. "</ul>") end
        table.insert(lines, "</div>")
        table.insert(parts, table.concat(lines))
    end
    if #parts == 0 then
        return "<p>" .. esc(_("Nothing here yet.")) .. "</p>"
    end
    return table.concat(parts)
end

local SHORT_LABELS = { ["c:summarize"] = _("Summarize"), ["c:continue"] = _("Continue"), ["c:stop"] = _("Stop") }

--- The Reply button: Continue / Summarize in one row at the top, then the suggested
--- replies, then Type… to write your own. With none of those it goes straight to typing.
function T3:showReplyDialog(view, quick, suggestions, can_type)
    if #quick == 0 and #suggestions == 0 then return self:typeReply(view) end
    local dialog
    local function closing(b)
        return {
            text = b.text,
            enabled = b.enabled,
            callback = function()
                UIManager:close(dialog)
                b.callback()
            end,
        }
    end
    local rows = {}
    if #quick > 0 then
        local row = {}
        for _i, b in ipairs(quick) do table.insert(row, closing(b)) end
        table.insert(rows, row)
    end
    for _i, b in ipairs(suggestions) do
        table.insert(rows, { closing(b) })
    end
    table.insert(rows, { {
        text = _("Type…"),
        enabled = can_type,
        callback = function()
            UIManager:close(dialog)
            self:typeReply(view)
        end,
    } })
    dialog = ButtonDialog:new{ title = _("Reply"), buttons = rows }
    UIManager:show(dialog)
end

function T3:threadButtons(view)
    local rows = {}
    local shortcuts = {}
    local answers = {}
    local suggestions = {} -- offered by the Reply button
    local quick = {} -- Continue / Summarize, at the top of the Reply dialog
    for _i, r in ipairs(view.replies or {}) do
        local id = str(r.id)
        local button = {
            text = SHORT_LABELS[id] or str(r.label) or id,
            enabled = r.enabled ~= false,
            callback = function()
                if r.kind == "secret" then return self:enterSecret(view) end
                self:sendReply(view, id, r.kind == "toggle")
            end,
        }
        if r.kind == "suggestion" then
            table.insert(suggestions, button)
        elseif r.kind == "shortcut" and id ~= "c:stop" then
            table.insert(quick, button)
        elseif r.kind == "shortcut" then
            table.insert(shortcuts, button) -- Stop stays on the thread screen
        elseif r.kind == "secret" then
            table.insert(answers, 1, button) -- Enter secret, then Decline, in one row
        elseif r.kind == "answer" and #(str(r.label) or "") <= 18 then
            table.insert(answers, button) -- short answers (Allow / Decline) share a row
        else
            table.insert(rows, { button })
        end
    end
    -- Short answers go first, all in one row.
    if #answers > 0 then table.insert(rows, 1, answers) end
    if #shortcuts > 0 then table.insert(rows, shortcuts) end
    local typed = str(view.typed) or "message"
    table.insert(rows, {
        {
            text = _("Reply"),
            enabled = typed ~= "none" or #suggestions > 0 or #quick > 0,
            callback = function() self:showReplyDialog(view, quick, suggestions, typed ~= "none") end,
        },
        {
            text = _("Model"),
            callback = function() self:changeModel(view) end,
        },
        {
            text = _("Browser"),
            callback = function() self:showBrowser(view) end,
        },
        {
            text = _("Refresh"),
            callback = function() self:reloadThread(view.id) end,
        },
    })
    return rows
end

-- ---------------------------------------------------------------------------
-- preview browser (view only)

--- The thread's preview browser: straight to the tab if there's one, else a list.
function T3:showBrowser(view)
    local ok, body = withBusy(_("Loading browser tabs…"), function() return self.api:browserTabs(view.id) end)
    if not ok then
        notify(body)
        return
    end
    local tabs = type(body.tabs) == "table" and body.tabs or {}
    if #tabs == 0 then
        notify(_("This thread has no browser tabs open."))
        return
    end
    if #tabs == 1 then return self:showBrowserTab(view, tabs[1]) end
    local dialog
    local rows = {}
    for _i, tab in ipairs(tabs) do
        local label = str(tab.title) or _("Blank tab")
        if tab.viewable == false then label = label .. " " .. _("(desktop app)") end
        table.insert(rows, {{
            text = label,
            align = "left",
            callback = function()
                UIManager:close(dialog)
                self:showBrowserTab(view, tab)
            end,
        }})
    end
    table.insert(rows, {{ text = _("Cancel"), callback = function() UIManager:close(dialog) end }})
    dialog = ButtonDialog:new{ title = _("Browser tabs"), buttons = rows }
    UIManager:show(dialog)
end

--- A snapshot of one tab, full screen, with Refresh to take a new one.
function T3:showBrowserTab(view, tab, old_viewer)
    if tab.viewable == false then
        notify(_("This tab is open in the desktop app's own window, which T3 can't share."))
        return
    end
    -- A new name each time: KOReader caches decoded images by file name.
    self.snapshot_n = (self.snapshot_n or 0) + 1
    local file = imageDir() .. "/browser-" .. os.time() .. "-" .. self.snapshot_n .. ".png"
    local ok, err = withBusy(_("Taking a snapshot…"), function()
        -- Sized for the long side, so the page stays sharp when rotated.
        local width = math.max(Device.screen:getWidth(), Device.screen:getHeight())
        return self.api:browserSnapshot(view.id, str(tab.id), width, file)
    end)
    if not ok then
        notify(err)
        return
    end
    local viewer
    viewer = ImageViewer:new{
        file = file,
        image_disposable = true,
        fullscreen = true,
        with_title_bar = true,
        title_text = str(tab.title) or _("Browser"),
        caption = str(tab.url),
        caption_visible = false,
        buttons_visible = true,
        rotated = old_viewer and old_viewer.rotated or nil,
    }
    -- ImageViewer's own buttons are Scale / Rotate / Close; put Refresh first.
    local ButtonTable = require("ui/widget/buttontable")
    viewer.button_table = ButtonTable:new{
        width = viewer.width - 2 * viewer.button_padding,
        zero_sep = true,
        show_parent = viewer,
        buttons = {{
            { text = _("Refresh"), callback = function() self:showBrowserTab(view, tab, viewer) end },
            { id = "scale", text = _("Scale"), callback = function()
                viewer.scale_factor = viewer._scale_to_fit and 1 or 0
                viewer._scale_to_fit = not viewer._scale_to_fit
                viewer._center_x_ratio = 0.5
                viewer._center_y_ratio = 0.5
                viewer:update()
            end },
            { id = "rotate", text = _("Rotate"), callback = function()
                viewer.rotated = not viewer.rotated
                viewer:update()
            end },
            { text = _("Close"), callback = function() viewer:onClose() end },
        }},
    }
    viewer.button_container[1] = viewer.button_table
    viewer:update()
    local onCloseWidget = viewer.onCloseWidget
    viewer.onCloseWidget = function(w)
        if onCloseWidget then onCloseWidget(w) end
        os.remove(file) -- already decoded; nothing reads it again
        if self.browser_viewer == w then -- not when Refresh replaced it
            self.browser_viewer = nil
            self:endTyping(false)
        end
    end
    -- Hold the thread's background redraws while the snapshot is open, as the reply box does.
    self.typing = true
    self.browser_viewer = viewer
    -- Show the new snapshot before closing the old one, so nothing flashes through.
    UIManager:show(viewer)
    if old_viewer then UIManager:close(old_viewer) end
end

--- Show a thread view. `opts.update`: redraw an already open thread quietly (no
--- e-ink flash), staying on the reader's page unless they were at the end.
--- `opts.page`: open on this page.
function T3:showThread(view, opts)
    opts = opts or {}
    local old = self.viewer
    self.viewer = nil
    local page = opts.page
    if not page and opts.update and old and not old:atLastPage() then
        page = old:currentPage()
    end
    -- At the end of the thread, a new reply opens at its start rather than its last page.
    local anchor = not page and anchorIndex(view, opts.update and old and self.view or nil) or nil
    local viewer
    viewer = ThreadView:new{
        title = str(view.title) or _("Thread"),
        html = composeHtml(view, self:fetchImages(view), anchor, self:pageBreakYou()),
        resource_dir = imageDir(),
        buttons = self:threadButtons(view),
        start_page = page,
        anchor_uri = anchor and ANCHOR or nil,
        refresh = (opts.update and old) and "ui" or "full",
        title_hold_callback = function() self:viewActions(view) end,
        link_callback = function(uri)
            if uri == "t3:older" then
                return self:showOlder(view, view.entries and view.entries[1] and view.entries[1].o)
            end
            local run = uri:match("^t3:run:(%w+)$")
            if run then return self:toggleRun(view, run) end
            local child = uri:match("^t3:thread:(%w+)$")
            if child then return self:openChild(view, child) end
            local image = uri:match("^t3:img:(%w+)$")
            if image then return self:showImage(image) end
        end,
        -- The title bar's X (or Back) closes the thread.
        close_callback = function()
            if self.viewer == viewer then
                self:stopThreadPoll()
                self.viewer = nil
                self.view = nil
                if self:returnToParent(view) then return end
                self:refreshListOnReturn()
            end
        end,
    }
    Gestures.attach(viewer)
    self.viewer = viewer
    self.view = view
    -- Show the new view before closing the old one, so nothing underneath flashes through.
    UIManager:show(viewer)
    if old then UIManager:close(old) end
    self:scheduleThreadPoll()
end

--- Open a subagent's thread from its parent. Closing it comes back to the parent.
function T3:openChild(parent_view, child_id)
    local ok, view = withBusy(_("Loading…"), function()
        return self.api:thread(child_id, { visit = 1, progress = self:progressParam(),
            expand = self:expandParam(child_id) })
    end)
    if not ok then
        notify(view)
        return
    end
    self.thread_stack = self.thread_stack or {}
    table.insert(self.thread_stack, { id = parent_view.id, page = self.viewer and self.viewer:currentPage() })
    self:showThread(view)
end

--- After closing a subagent thread, reopen the thread it came from.
--- Returns true if it did.
function T3:returnToParent(view)
    local stack = self.thread_stack or {}
    local parent = table.remove(stack)
    if not parent and type(view.parent) == "table" and str(view.parent.id) then
        parent = { id = view.parent.id } -- opened some other way: still go up a level
    end
    if not parent then return false end
    UIManager:nextTick(function() self:openThread(parent.id, parent.page) end)
    return true
end

function T3:reloadThread(id, page)
    local ok, view = withBusy(_("Refreshing…"), function()
        return self.api:thread(id, { progress = self:progressParam(), expand = self:expandParam(id) })
    end)
    if not ok then
        notify(view)
        return
    end
    self:showThread(view, { update = true, page = page })
end

function T3:stopThreadPoll()
    if self.thread_poll then
        UIManager:unschedule(self.thread_poll)
        self.thread_poll = nil
    end
end

function T3:scheduleThreadPoll()
    self:stopThreadPoll()
    local view = self.view
    if not view then return end
    local busy = view.state == "working" or view.state == "needs_you"
    local interval = busy and POLL_ACTIVE or POLL_IDLE
    if view.state == "working" and self:showProgress() then interval = POLL_PROGRESS end
    self.thread_poll = function()
        self.thread_poll = nil
        if self.view ~= view then return end
        if NetworkMgr:isConnected() then
            local ok, body = self.api:changedSince(view.id, view.v, self:progressParam(),
                self:expandParam(view.id))
            if self.view ~= view then return end
            if ok and body.changed == true then
                if self.typing then
                    -- Don't draw over the text box; show it when the box closes.
                    self.held_view = body
                    view.v = body.v
                else
                    self:showThread(body, { update = true })
                    return
                end
            end
        end
        self:scheduleThreadPoll()
    end
    UIManager:scheduleIn(interval, self.thread_poll)
end

function T3:sendReply(view, reply_id, quiet)
    local function go()
        local function send()
            return self.api:reply(view.id, reply_id, nil, self:showProgress(), self:expandList(view.id))
        end
        -- Ticking an option is instant; a "Sending…" popup would only add flicker.
        local ok, result
        if quiet then ok, result = send() else ok, result = withBusy(_("Sending…"), send) end
        if ok then
            self:showThread(result, { update = true, page = math.huge })
        else
            notify(result)
            -- A 409 means the buttons were stale; show the current state.
            self:reloadThread(view.id)
        end
    end
    if reply_id == "c:stop" then
        UIManager:show(ConfirmBox:new{
            text = _("Stop the agent?"),
            ok_text = _("Stop"),
            ok_callback = go,
        })
    else
        go()
    end
end

function T3:typeReply(view)
    local typed = str(view.typed) or "message"
    local p = type(view.pending) == "table" and view.pending or nil
    -- The box covers the thread, so its title bar names the thread; a question being
    -- answered goes under it.
    local question = typed == "answer" and p and (str(p.text) or _("Answer")) or nil
    -- A message (not an answer) can switch plan mode, as the app's Plan toggle does.
    local plan
    if typed == "message" and view.plan_mode == true then
        plan = self:planToggle(str(view.mode) == "plan")
    end
    self:compose{
        title = str(view.title) or _("Reply"),
        description = question,
        hint = typed == "answer" and _("Your answer") or _("Message"),
        send_text = _("Send"),
        extras = { plan and plan.button },
        on_send = function(text)
            local mode = plan and (plan.on and "plan" or "default") or nil
            local ok, result = withBusy(_("Sending…"), function()
                return self.api:reply(view.id, nil, text, self:showProgress(), self:expandList(view.id), mode)
            end)
            if ok then
                self:showThread(result, { update = true, page = math.huge })
            else
                notify(result)
            end
        end,
    }
end

-- ---------------------------------------------------------------------------
-- models

--- Choose a model. `params`: {thread = id} or {project = id}, for the current model.
--- Shows the desktop's favorites, then "More models…" for the rest; calls
--- on_pick(id, label) with the choice.
function T3:pickModel(params, on_pick)
    local ok, body = withBusy(_("Loading models…"), function() return self.api:models(params) end)
    if not ok then
        notify(body)
        return
    end
    local current = str(body.current)
    local function mark(m)
        return (m.id == current and "● " or "") .. (str(m.label) or m.id)
    end
    local groups = body.groups or {}
    local favorites = body.favorites or {}
    if #favorites == 0 then
        if #groups == 0 then return notify(_("No models available.")) end
        return self:showAllModels(groups, mark, on_pick)
    end
    local dialog
    local rows = {}
    for _i, m in ipairs(favorites) do
        table.insert(rows, {{
            text = mark(m),
            callback = function()
                UIManager:close(dialog)
                on_pick(m.id, str(m.label))
            end,
        }})
    end
    if #groups > 0 then
        table.insert(rows, {{
            text = _("More models…"),
            callback = function()
                UIManager:close(dialog)
                self:showAllModels(groups, mark, on_pick)
            end,
        }})
    end
    local title = _("Model")
    if str(body.current_label) then title = T(_("Model: %1"), body.current_label) end
    dialog = ButtonDialog:new{ title = title, buttons = rows }
    UIManager:show(dialog)
end

--- Every model that isn't a favorite, under its provider's name.
function T3:showAllModels(groups, mark, on_pick)
    local menu
    local items = {}
    for _i, g in ipairs(groups) do
        table.insert(items, { text = str(g.title) or "", bold = true })
        for _j, m in ipairs(g.models or {}) do
            table.insert(items, {
                text = "    " .. mark(m),
                model = m,
            })
        end
    end
    menu = Menu:new{
        title = _("Models"),
        item_table = items,
        is_borderless = true,
        is_popout = false,
        covers_fullscreen = true,
        close_callback = function() UIManager:close(menu) end,
    }
    menu.onMenuSelect = function(_menu, item)
        if not item.model then return true end -- a provider heading
        UIManager:close(menu)
        on_pick(item.model.id, str(item.model.label))
        return true
    end
    UIManager:show(menu)
end

--- The thread's Model button: switch the model its next turn uses.
function T3:changeModel(view)
    self:pickModel({ thread = view.id }, function(model)
        local ok, result = withBusy(_("Switching model…"), function()
            return self.api:setModel(view.id, model, self:showProgress())
        end)
        if ok then
            self:showThread(result, { update = true })
        else
            notify(result)
        end
    end)
end

--- Ask for the value of a secret request in a masked box. The value goes to the bridge
--- in one request and isn't kept anywhere on the device.
function T3:enterSecret(view)
    local p = type(view.pending) == "table" and view.pending or {}
    local dialog
    dialog = InputDialog:new{
        title = str(p.header) or _("Secret"),
        description = str(p.text),
        input = "",
        input_hint = str(p.placeholder) or _("Secret"),
        text_type = "password",
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            {
                text = _("Save"),
                is_enter_default = true,
                callback = function()
                    local secret = dialog:getInputText()
                    if not secret:match("%S") then return end
                    dialog._t3_sent = true
                    UIManager:close(dialog)
                    local ok, result = withBusy(_("Saving…"), function()
                        return self.api:answerSecret(view.id, secret)
                    end)
                    secret = nil
                    if ok then
                        self:showThread(result, { update = true, page = math.huge })
                    else
                        notify(result)
                        self:reloadThread(view.id)
                    end
                end,
            },
        }},
    }
    dialog.onCloseWidget = function(d)
        InputDialog.onCloseWidget(d)
        self:endTyping(d._t3_sent)
    end
    self.typing = true -- hold background redraws while the box is open
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

--- Relabel a button of the open text box (and keep the label if the box is rebuilt).
function T3:setComposeButton(b, text)
    b.text = text
    local dialog = self.compose_dialog
    local button = dialog and dialog.button_table and dialog.button_table:getButtonById(b.id)
    if button then
        button:setText(text, button.width)
        UIManager:setDirty(dialog, "ui")
    end
end

--- A Plan toggle for a text box: {on = bool, button = the box's button}.
function T3:planToggle(on)
    local toggle = { on = on }
    local function label() return (toggle.on and "● " or "○ ") .. _("Plan") end
    toggle.button = {
        text = label(),
        id = "plan",
        callback = function()
            toggle.on = not toggle.on
            self:setComposeButton(toggle.button, label())
        end,
    }
    return toggle
end

--- A text box that fills the screen (full width, down to the keyboard). The thread behind it is
--- held still while it's open (background checks don't redraw over it), so there's
--- nothing worth leaving visible.
function T3:compose(o)
    local dialog
    -- Cancel, optional extra buttons (o.extras), then Send.
    local row = {
        { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
    }
    for _i, b in ipairs(o.extras or {}) do table.insert(row, b) end
    table.insert(row, {
        text = o.send_text,
        is_enter_default = true,
        callback = function()
            local text = dialog:getInputText()
            if not text:match("%S") then return end
            dialog._t3_sent = true
            UIManager:close(dialog)
            o.on_send(text)
        end,
    })
    dialog = InputDialog:new{
        title = o.title,
        description = o.description,
        input = o.input or "",
        input_hint = o.hint,
        allow_newline = not o.one_line,
        width = Device.screen:getWidth(),
        use_available_height = not o.one_line,
        buttons = { row },
    }
    dialog.onCloseWidget = function(d)
        InputDialog.onCloseWidget(d)
        if self.compose_dialog == d then self.compose_dialog = nil end
        self:endTyping(d._t3_sent)
    end
    self.compose_dialog = dialog
    self.typing = true
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

--- The text box closed: show any thread update that arrived while it was open.
--- `sent`: the caller is sending the text and will show the result, which is
--- newer than anything held, so drop the held update.
function T3:endTyping(sent)
    self.typing = false
    local held = self.held_view
    self.held_view = nil
    if sent or not held then return end
    UIManager:nextTick(function()
        if self.view and held.id == self.view.id and not self.typing then
            self:showThread(held, { update = true })
        end
    end)
end

function T3:showOlder(view, before)
    if type(before) ~= "number" then return end
    local ok, older = withBusy(_("Loading…"), function()
        return self.api:thread(view.id, { before = before, turns = 3, expand = self:expandParam(view.id) })
    end)
    if not ok then
        notify(older)
        return
    end
    older.state = "done" -- an archive page: no live status line
    older.pending = nil
    local first = older.entries and older.entries[1] and older.entries[1].o
    local older_viewer
    older_viewer = ThreadView:new{
        title = T(_("%1 (earlier)"), str(view.title) or _("Thread")),
        html = composeHtml(older, self:fetchImages(older), nil, self:pageBreakYou()),
        resource_dir = imageDir(),
        link_callback = function(uri)
            if uri == "t3:older" then
                UIManager:close(older_viewer)
                self:showOlder(view, first)
            end
        end,
    }
    Gestures.attach(older_viewer)
    UIManager:show(older_viewer)
end

-- ---------------------------------------------------------------------------
-- new thread

function T3:chooseProject()
    local ok, body = withBusy(_("Loading projects…"), function() return self.api:projects() end)
    if not ok then
        notify(body)
        return
    end
    local dialog
    local rows = {}
    for _i, p in ipairs(body.projects or {}) do
        table.insert(rows, {{
            text = str(p.title) or p.id,
            callback = function()
                UIManager:close(dialog)
                self:newThread(p)
            end,
        }})
    end
    if #rows == 0 then
        notify(_("No projects."))
        return
    end
    dialog = ButtonDialog:new{ title = _("New thread in…"), buttons = rows }
    UIManager:show(dialog)
end

function T3:newThread(project)
    local model -- nil: the project's default model, from T3's settings
    local extra
    extra = {
        text = _("Model"),
        id = "model",
        callback = function()
            -- Put the keyboard away first: while it's up, the first tap on the
            -- picker only closes it.
            local dialog = self.compose_dialog
            if dialog and dialog:isKeyboardVisible() then dialog:onCloseKeyboard() end
            self:pickModel({ project = project.id }, function(id, label)
                model = id
                -- Show the choice on the button.
                self:setComposeButton(extra, label or _("Model"))
                dialog = self.compose_dialog
                if dialog then dialog:onShowKeyboard() end
            end)
        end,
    }
    local plan = self:planToggle(false)
    self:compose{
        title = T(_("New thread in %1"), str(project.title) or ""),
        hint = _("What should the agent do?"),
        send_text = _("Start"),
        extras = { extra, plan.button },
        on_send = function(text)
            local ok, view = withBusy(_("Starting…"), function()
                return self.api:startThread(project.id, text, model, plan.on and "plan" or nil)
            end)
            if ok then
                self:showThread(view)
            else
                notify(view)
            end
        end,
    }
end

function T3:onCloseWidget()
    self:stopThreadPoll()
    if self.list_poll then UIManager:unschedule(self.list_poll) end
end

return T3
