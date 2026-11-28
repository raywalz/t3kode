--[[--
T3 Code client for KOReader.

Shows T3 Code threads from a T3 KOde bridge and lets you reply with a tap.
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
local TitleBar = require("ui/widget/titlebar")
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
-- The link at the top of a thread loads this many earlier turns at a time, up
-- to MAX_TURNS in all; beyond that, earlier turns open on a screen of their own.
local LOAD_TURNS = 3
local MAX_TURNS = 30
-- The plugin's Api (set by T3:makeApi), so ensureConnected can check the bridge answers.
-- Declared up here, above T3:makeApi, so the assignment there reaches this local.
local bridge_api

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
        category = "none", event = "T3Open", title = _("T3 KOde threads"), general = true,
    })
    Dispatcher:registerAction("t3_reload", {
        category = "none", event = "T3Reload", title = _("Reload T3 KOde"), general = true,
    })
    self.ui.menu:registerToMainMenu(self)
end

function T3:makeApi()
    self.api = Api.new(self.settings:readSetting("bridge_url"), self.settings:readSetting("bridge_token"))
    bridge_api = self.api
end

-- Put "T3 KOde" first in the Tools tab of both the file browser and the reader.
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
        text = _("T3 KOde"),
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
        title = _("T3 KOde settings"),
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

local callLabel -- a call's status line (voice replies, below); used by the thread's buttons

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
    self.voice_on = body.call == true -- the bridge can take a new thread's first message by phone
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
    -- Starting a thread is the main thing to do here, so it's the first row rather than
    -- only in the menu. In a project's list it starts in that project.
    table.insert(items, 1, {
        text = "+  " .. _("New thread"),
        bold = true,
        callback = function()
            if project_filter then
                self:newThread({ id = project_filter, title = self.list and self.list.title })
            else
                self:chooseProject()
            end
        end,
    })
    -- A problem with the bridge itself (e.g. its T3 token is about to expire) goes on top.
    if str(body.notice) then
        table.insert(items, 1, { text = "! " .. str(body.notice), bold = true })
    end
    local update = type(body.update) == "table" and self:updateRow(body.update) or nil
    if update then table.insert(items, 1, update) end
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
        title = title or _("T3 KOde"),
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
        if not self.viewer and not self.typing and not self.pr_list and NetworkMgr:isConnected() then
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
    end, { text = _("Reload T3 KOde"), callback = function() self:reloadPlugin() end })
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
            {{ text = _("Pull requests"), callback = function()
                UIManager:close(dialog)
                self:showPullRequests()
            end }},
            {{ text = _("Usage"), callback = function()
                UIManager:close(dialog)
                self:showUsage()
            end }},
            {{ text = _("Refresh"), callback = function()
                UIManager:close(dialog)
                withBusy(_("Refreshing…"), function() self:refreshList(true) end)
            end }},
            {{ text = _("Settings"), callback = function()
                UIManager:close(dialog)
                self:showSettings()
            end }},
            {{ text = _("Reload T3 KOde"), callback = function()
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
            notify(_("T3 KOde reloaded."), 2)
        else
            notify(T(_("Couldn't reload T3 KOde; still running the old version.\n%1"), tostring(new)), 10)
        end
    end)
end

-- ---------------------------------------------------------------------------
-- updating: a newer release updates the bridge (it tests, updates and restarts itself),
-- then the plugin updates from the bridge's copy, so the two always match.

local function versionParts(v)
    local a, b, c = tostring(v or ""):match("^v?(%d+)%.(%d+)%.(%d+)$")
    if a then return { tonumber(a), tonumber(b), tonumber(c) } end
end

--- True when version a is newer than version b.
local function isNewer(a, b)
    local x, y = versionParts(a), versionParts(b)
    if not x or not y then return false end
    for i = 1, 3 do
        if x[i] ~= y[i] then return x[i] > y[i] end
    end
    return false
end

--- Delete a folder this plugin made next to itself (only those: see T3:updatePlugin).
local function removeTree(path)
    if not (path:match("%.koplugin%.new$") or path:match("%.koplugin%.old$")) then return end
    local function remove(p)
        local mode = lfs.attributes(p, "mode")
        if mode == "directory" then
            for name in lfs.dir(p) do
                if name ~= "." and name ~= ".." then remove(p .. "/" .. name) end
            end
            lfs.rmdir(p)
        elseif mode then
            os.remove(p)
        end
    end
    remove(path)
end

function T3:pluginVersion()
    local ok, meta = pcall(dofile, self.path .. "/_meta.lua")
    return ok and type(meta) == "table" and meta.version or nil
end

--- The list's top row for an update, or nil when there's nothing to say.
function T3:updateRow(up)
    local status = str(up.status)
    if status == "updating" or status == "restarting" then
        return { text = _("Updating T3 KOde…"), bold = true }
    elseif status == "failed" then
        return { text = "! " .. _("The update didn't work. Tap for why."), bold = true, callback = function()
            local why = T(_("Nothing was changed: %1."), str(up.message) or _("no reason given"))
            if str(up.release) and up.self_update == true then
                UIManager:show(ConfirmBox:new{
                    text = why, ok_text = _("Try again"),
                    ok_callback = function() self:updateBridge(up.release) end,
                })
            else
                notify(why, 20)
            end
        end }
    elseif str(up.release) then
        return { text = "↑  " .. T(_("T3 KOde %1 is available"), up.release), bold = true, callback = function()
            self:confirmUpdate(up)
        end }
    elseif isNewer(up.plugin, self:pluginVersion()) then
        return { text = "↑  " .. T(_("Update T3 KOde on this e-reader to %1"), up.plugin), bold = true, callback = function()
            self:updatePlugin()
        end }
    end
end

function T3:confirmUpdate(up)
    if up.self_update ~= true then
        notify(str(up.manual) or _("Update the bridge on the computer running it: git pull, then restart it."), 20)
        return
    end
    UIManager:show(ConfirmBox:new{
        text = T(_("Update T3 KOde to %1?\n\nThe bridge tests the new version, updates and restarts. Then T3 KOde on this e-reader updates too. It takes a minute or two."), up.release),
        ok_text = _("Update"),
        ok_callback = function() self:updateBridge(up.release) end,
    })
end

function T3:updateBridge(release)
    local ok, err = withBusy(_("Starting the update…"), function()
        return self.api:request("POST", "/v1/update", nil, { action = "update" })
    end)
    if not ok then
        notify(T(_("Couldn't start the update: %1"), err), 10)
        return
    end
    local msg = InfoMessage:new{
        text = T(_("Updating T3 KOde to %1…\nThe bridge tests the new version first, then restarts."), release),
        dismissable = false,
    }
    UIManager:show(msg)
    local deadline = os.time() + 15 * 60
    local function poll()
        -- While the bridge restarts, requests fail; keep asking.
        local got, info = self.api:request("GET", "/v1/update", nil, nil, { 4, 8 })
        if got and str(info.status) == "failed" then
            UIManager:close(msg)
            notify(T(_("The update didn't work. Nothing was changed: %1."), str(info.message) or _("no reason given")), 20)
            pcall(function() self:refreshList(true) end)
        elseif got and info.bridge == release then
            UIManager:close(msg)
            self:updatePlugin()
        elseif os.time() > deadline then
            UIManager:close(msg)
            notify(_("The bridge hasn't come back after updating. Check it on the computer running it."), 20)
        else
            UIManager:scheduleIn(3, poll)
        end
    end
    UIManager:scheduleIn(3, poll)
end

--- Download the bridge's copy of the plugin next to this one, check every file, then
--- swap the folders and reload. Any failure leaves the running plugin as it was.
function T3:updatePlugin()
    local sha2 = require("ffi/sha2")
    local new_dir, old_dir = self.path .. ".new", self.path .. ".old"
    local ok, err = withBusy(_("Updating T3 KOde on this e-reader…"), function()
        local got, manifest = self.api:request("GET", "/v1/plugin")
        if not got then return false, manifest end
        if type(manifest.files) ~= "table" or #manifest.files == 0 then
            return false, _("The bridge sent no plugin files.")
        end
        removeTree(new_dir)
        if not lfs.mkdir(new_dir) then return false, T(_("Can't write %1"), new_dir) end
        for _i, f in ipairs(manifest.files) do
            local name = str(f.path)
            if not name or not name:match("^[%w%._%-]+$") or name:sub(1, 1) == "." then
                return false, _("The bridge sent an odd file name.")
            end
            local target = new_dir .. "/" .. name
            local done, derr = self.api:download("/v1/plugin/files/" .. name, nil, target)
            if not done then return false, derr end
            local fh = io.open(target, "rb")
            local data = fh and fh:read("*a")
            if fh then fh:close() end
            if not data or #data ~= tonumber(f.size) or sha2.sha256(data) ~= f.sha256 then
                return false, T(_("%1 didn't arrive intact."), name)
            end
        end
        removeTree(old_dir)
        local moved, merr = os.rename(self.path, old_dir)
        if not moved then return false, merr end
        local placed, perr = os.rename(new_dir, self.path)
        if not placed then
            os.rename(old_dir, self.path)
            return false, perr
        end
        removeTree(old_dir)
        return true
    end)
    if not ok then
        removeTree(new_dir)
        notify(T(_("Couldn't update T3 KOde on this e-reader, so it's unchanged: %1"), tostring(err)), 15)
        return
    end
    logger.info("t3: plugin updated to", self:pluginVersion())
    self:reloadPlugin()
end

-- ---------------------------------------------------------------------------
-- thread view

--- `pin`: reopen with the earlier turns that were loaded (see T3:activePin).
function T3:openThread(id, page, pin)
    local ok, view = withBusy(_("Loading…"), function()
        return self:fetchView(id, { visit = 1, progress = self:progressParam(), expand = self:expandParam(id) },
            pin)
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
local SEAM = "t3:seam" -- where the turns already on screen start, after loading earlier ones

local function turnCount(view)
    local n = 0
    for _i, e in ipairs(view.entries or {}) do
        if e.role == "you" then n = n + 1 end
    end
    return n
end

--- The first labelled entry at or after cursor `o`: the seam's link goes on its label.
local function seamIndex(view, o)
    for i, e in ipairs(view.entries or {}) do
        if type(e.o) == "number" and e.o >= o and ROLE_LABEL[e.role] then return i end
    end
end

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

local function composeHtml(view, have, anchor, page_break_you, seam)
    local parts = {}
    if view.more_before == true then
        local text = turnCount(view) >= MAX_TURNS and _("⋯ Earlier messages (open separately)")
            or _("⋯ Show earlier messages")
        table.insert(parts, '<div class="more"><a href="t3:older">' .. esc(text) .. "</a></div>")
    end
    for i, e in ipairs(view.entries or {}) do
        local role = str(e.role) or "note"
        local body = str(e.html) or ("<p>" .. esc(str(e.text)) .. "</p>")
        body = placeImages(body, have or {})
        local label = ROLE_LABEL[role]
        if role == "fold" then label = nil end
        label = label and esc(label)
        if label and i == anchor then label = '<a href="' .. ANCHOR .. '">' .. label .. "</a>" end
        if label and i == seam then label = '<a href="' .. SEAM .. '">' .. label .. "</a>" end
        -- Optionally each of your messages starts a new page, so a turn reads from the top.
        local newpage = (page_break_you and role == "you" and i > 1) and " newpage" or ""
        table.insert(parts, '<div class="entry ' .. role .. newpage .. '">'
            .. (label and ('<div class="label">' .. label .. "</div>") or "")
            .. body .. "</div>")
    end
    local state = str(view.state)
    if state == "working" then
        -- Like the app's live row: the reply being written, then how long the turn has
        -- run and the step running now. (The turn's thoughts come as "thought" entries.)
        local live = type(view.live) == "table" and view.live or {}
        local draft = str(live.draft_html) or (str(live.draft_text) and "<p>" .. esc(live.draft_text) .. "</p>")
        if draft then
            table.insert(parts, '<div class="entry agent"><div class="label">' .. esc(_("Agent (writing…)"))
                .. "</div>" .. placeImages(draft, have or {}) .. "</div>")
        end
        local line = str(live.elapsed) and T(_("Working for %1"), live.elapsed) or _("Working…")
        if str(live.now) then line = line .. " · " .. live.now end
        table.insert(parts, '<div class="status">' .. esc(line) .. "</div>")
    elseif state == "failed" then
        table.insert(parts, '<div class="status">' .. esc(_("The last run failed.")) .. "</div>")
    end
    -- The agent's task list, like the app's "Tasks 3/7" banner: done steps ticked and
    -- struck through (by CSS: MuPDF ignores <s>), the running one in bold.
    local tasks = type(view.tasks) == "table" and view.tasks or nil
    if tasks and type(tasks.steps) == "table" and #tasks.steps > 0 then
        local rows = {}
        for _i, s in ipairs(tasks.steps) do
            local text = esc(str(s.text) or "")
            local status = str(s.status)
            if status == "completed" then
                table.insert(rows, '<p class="done">✓ ' .. text .. "</p>")
            elseif status == "running" then
                table.insert(rows, "<p>▸ <b>" .. text .. "</b></p>")
            else
                table.insert(rows, "<p>• " .. text .. "</p>")
            end
        end
        local count = string.format("%s/%s", tostring(tasks.done or 0), tostring(tasks.total or #tasks.steps))
        table.insert(parts, '<div class="prs tasks"><div class="pending-title">' .. esc(T(_("Tasks %1"), count))
            .. "</div>" .. table.concat(rows) .. "</div>")
    end
    -- Linked PRs, as T3 last synced them: "PR #12 open · checks pass · needs review".
    -- (T3 checks open PRs every few minutes, so there's no "as of" to show.)
    local prs = {}
    for i, pr in ipairs(type(view.prs) == "table" and view.prs or {}) do
        local number = type(pr.number) == "number" and tostring(pr.number) or str(pr.number) or "?"
        local line = "<b>#" .. esc(number) .. "</b> " .. esc(str(pr.status) or "")
        if str(pr.title) then line = line .. " · <i>" .. esc(str(pr.title)) .. "</i>" end
        -- Tap to open it (bridges before PR pages send no project).
        if str(pr.project) then line = '<a href="t3:pr:' .. i .. '">' .. line .. " ›</a>" end
        table.insert(prs, "<p>" .. line .. "</p>")
    end
    if #prs > 0 then
        local heading = #prs > 1 and _("Pull requests") or _("Pull request")
        table.insert(parts, '<div class="prs"><div class="pending-title">' .. esc(heading)
            .. "</div>" .. table.concat(prs) .. "</div>")
    end
    -- How full the agent's context window is, like the app's context meter.
    local context = type(view.context) == "table" and str(view.context.label)
    if context then
        table.insert(parts, '<div class="status">' .. esc(context) .. "</div>")
    end
    if str(view.mode) == "plan" then
        table.insert(parts, '<div class="status">' .. esc(_("Plan mode is on.")) .. "</div>")
    end
    -- Messages waiting for the running turn to end, like the app's queue above its
    -- composer. Tap one to send it now / steer with it, or remove it.
    local queue = type(view.queue) == "table" and view.queue or nil
    if queue and type(queue.items) == "table" and #queue.items > 0 then
        local rows = {}
        for _i, item in ipairs(queue.items) do
            local text = str(item.text) or ""
            if #text > 240 then text = text:sub(1, 239):gsub("[\128-\191]*$", ""):gsub("[\192-\255]$", "") .. "…" end
            table.insert(rows, '<p><a href="t3:queue:' .. esc(str(item.id) or "") .. '">▸ ' .. esc(text) .. "</a></p>")
        end
        if queue.held == true then
            table.insert(rows, '<p><a href="t3:queue:resume"><b>' .. esc(_("Resume queue ›")) .. "</b></a></p>")
        end
        local heading = queue.held == true and T(_("Queued (%1) · paused"), #queue.items)
            or T(_("Queued (%1)"), #queue.items)
        table.insert(parts, '<div class="prs"><div class="pending-title">' .. esc(heading) .. "</div>"
            .. table.concat(rows) .. "</div>")
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
    if #quick == 0 and #suggestions == 0 and view.call ~= true then return self:typeReply(view) end
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
    local type_row = { {
        text = _("Type…"),
        enabled = can_type,
        callback = function()
            UIManager:close(dialog)
            self:typeReply(view)
        end,
    } }
    if view.call == true and can_type then
        table.insert(type_row, {
            text = _("Call me"),
            callback = function()
                UIManager:close(dialog)
                self:callReply(view)
            end,
        })
    end
    table.insert(rows, type_row)
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
            text = _("Preview"),
            callback = function() self:showBrowser(view) end,
        },
        {
            -- Same as the title bar's X: back to the parent thread or the list.
            text = _("Close"),
            callback = function() if self.viewer then self.viewer:onClose() end end,
        },
    })
    -- On a call for this thread: its status and Hang up on top, so the thread stays
    -- readable (and scrollable) while you talk.
    local call = self.call
    if call and call.thread == view.id then
        table.insert(rows, 1, {
            { text = callLabel(call.state), id = "call_status", callback = function() end }, -- a label
            { text = _("Hang up"), callback = function() call.hang_up() end },
        })
    end
    return rows
end

-- ---------------------------------------------------------------------------
-- preview: browser tabs and simulators/emulators (view only)

--- What the Preview button can show, as { title, caption, viewable, fetch(width, file) }.
local function previewItems(api, view, body)
    local items = {}
    for _i, tab in ipairs(type(body.tabs) == "table" and body.tabs or {}) do
        local title = str(tab.title) or _("Blank tab")
        table.insert(items, {
            title = title,
            label = tab.viewable == false and (title .. " " .. _("(desktop app)")) or title,
            caption = str(tab.url),
            viewable = tab.viewable ~= false,
            fetch = function(width, file) return api:browserSnapshot(view.id, str(tab.id), width, file) end,
        })
    end
    for _i, dev in ipairs(type(body.devices) == "table" and body.devices or {}) do
        local name = str(dev.name) or str(dev.id) or _("Device")
        local kind = str(dev.kind)
        table.insert(items, {
            title = name,
            label = kind and (name .. " (" .. kind .. ")") or name,
            caption = kind,
            viewable = true,
            fetch = function(width, file) return api:deviceSnapshot(view.id, str(dev.id), width, file) end,
        })
    end
    return items
end

--- The thread's previews: straight to the one if there's one, else a list.
function T3:showBrowser(view)
    local ok, body = withBusy(_("Loading previews…"), function() return self.api:browserTabs(view.id) end)
    if not ok then
        notify(body)
        return
    end
    local items = previewItems(self.api, view, type(body) == "table" and body or {})
    if #items == 0 then
        notify(_("This thread has no browser tabs or devices open."))
        return
    end
    if #items == 1 then return self:showPreview(view, items[1]) end
    local dialog
    local rows = {}
    for _i, item in ipairs(items) do
        table.insert(rows, {{
            text = item.label,
            align = "left",
            callback = function()
                UIManager:close(dialog)
                self:showPreview(view, item)
            end,
        }})
    end
    table.insert(rows, {{ text = _("Cancel"), callback = function() UIManager:close(dialog) end }})
    dialog = ButtonDialog:new{ title = _("Preview"), buttons = rows }
    UIManager:show(dialog)
end

--- A snapshot of one tab or device, full screen, with Refresh to take a new one.
function T3:showPreview(view, item, old_viewer)
    if not item.viewable then
        notify(_("This tab is open in the desktop app's own window, which T3 can't share."))
        return
    end
    -- A new name each time: KOReader caches decoded images by file name.
    self.snapshot_n = (self.snapshot_n or 0) + 1
    local file = imageDir() .. "/preview-" .. os.time() .. "-" .. self.snapshot_n .. ".png"
    local ok, err = withBusy(_("Taking a snapshot…"), function()
        -- Sized for the long side, so the page stays sharp when rotated.
        local width = math.max(Device.screen:getWidth(), Device.screen:getHeight())
        return item.fetch(width, file)
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
        title_text = item.title,
        caption = item.caption,
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
            { text = _("Refresh"), callback = function() self:showPreview(view, item, viewer) end },
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

-- ---------------------------------------------------------------------------
-- pull requests (view only)

-- A list row has little room beside the title, so its status is short: "✓ approved".
local PR_SHORT = {
    ["open"] = false, ["needs review"] = false, -- what almost every row would say
    ["checks pass"] = "✓", ["checks fail"] = "✗", ["checks running"] = "…",
    ["changes requested"] = _("changes"),
}

local function prStatus(pr)
    local parts = {}
    for part in (str(pr.status) or ""):gmatch("[^·]+") do
        part = part:gsub("^%s+", ""):gsub("%s+$", "")
        local short = PR_SHORT[part]
        if short == nil then short = part end
        if short then table.insert(parts, short) end
    end
    return table.concat(parts, " ")
end

function T3:prListItems(body)
    local items = {}
    local mine = body.scope ~= "all"
    for _i, g in ipairs(type(body.groups) == "table" and body.groups or {}) do
        local prs = type(g.prs) == "table" and g.prs or {}
        table.insert(items, { text = (str(g.title) or "") .. "  (" .. #prs .. ")", bold = true })
        -- "Yours" can mix projects; then each row says which. Everyone's is grouped by project.
        local projects = {}
        for _j, pr in ipairs(prs) do projects[str(pr.project_title) or ""] = true end
        local mixed = mine and next(projects, next(projects)) ~= nil
        for _j, pr in ipairs(prs) do
            local number = "#" .. tostring(pr.number or "?")
            local where = mixed and str(pr.project_title) and (str(pr.project_title) .. " ") or ""
            local status = prStatus(pr)
            table.insert(items, {
                text = "    " .. where .. number .. " · " .. (str(pr.title) or ""),
                mandatory = (status ~= "" and (status .. "  ") or "") .. ago(pr.updated),
                dim = pr.draft == true or nil,
                callback = function() self:showPullRequest(pr) end,
            })
        end
    end
    if #items == 0 then
        table.insert(items, { text = mine and _("No open pull requests of yours or waiting for your review.")
            or _("No open pull requests.") })
    end
    if body.truncated == true then
        table.insert(items, { text = _("Only the latest are shown; see the rest in T3 on the desktop."), dim = true })
    end
    local errors = type(body.errors) == "table" and body.errors or {}
    if #errors > 0 then
        table.insert(items, {
            text = T(_("%1 projects couldn't be read ›"), #errors),
            dim = true,
            callback = function()
                local lines = {}
                for _i, e in ipairs(errors) do
                    table.insert(lines, (str(e.project) or "?") .. ": " .. (str(e.message) or ""))
                end
                UIManager:show(InfoMessage:new{ text = table.concat(lines, "\n\n") })
            end,
        })
    end
    return items
end

--- The pull requests screen: yours and those waiting for your review (`scope` "mine"),
--- or everyone's open ones ("all"), as T3's Pull requests page lists them.
function T3:showPullRequests(scope)
    scope = scope or "mine"
    local ok, body = withBusy(scope == "all" and _("Loading all open pull requests…")
        or _("Loading pull requests…"), function() return self.api:pullRequests(scope) end)
    if not ok then
        notify(body)
        return
    end
    local old = self.pr_list
    local menu
    menu = Menu:new{
        title = scope == "all" and _("All open pull requests") or _("Pull requests"),
        item_table = self:prListItems(body),
        is_popout = false,
        is_borderless = true,
        covers_fullscreen = true,
        title_bar_left_icon = "appbar.menu",
        close_callback = function()
            UIManager:close(menu)
            if self.pr_list == menu then self.pr_list = nil end
        end,
    }
    menu.onMenuSelect = function(_menu, item)
        if item.callback then item.callback() end
        return true
    end
    menu.onLeftButtonTap = function()
        local dialog
        dialog = ButtonDialog:new{
            buttons = {
                {{ text = scope == "all" and _("Yours and review requests") or _("All open pull requests"),
                    callback = function()
                        UIManager:close(dialog)
                        self:showPullRequests(scope == "all" and "mine" or "all")
                    end }},
                {{ text = _("Refresh"), callback = function()
                    UIManager:close(dialog)
                    self:showPullRequests(scope)
                end }},
            },
        }
        UIManager:show(dialog)
    end
    Gestures.attach(menu)
    -- The thread list underneath doesn't redraw while this is open (see scheduleListPoll).
    self.pr_list = menu
    UIManager:show(menu)
    if old then UIManager:close(old) end
end

--- One pull request: status, checks, description, conversation, commits and files.
--- `pr`: {project, repo, number, host} from the list or a thread's linked PRs.
function T3:showPullRequest(pr, old_page)
    local ok, body = withBusy(_("Loading pull request…"), function() return self.api:pullRequest(pr) end)
    if not ok then
        notify(body)
        return
    end
    local page
    page = ThreadView:new{
        title = str(body.title) or ("#" .. tostring(pr.number)),
        html = str(body.html) or "",
        start_at_bottom = false,
        start_page = old_page and old_page:currentPage() or nil,
        refresh = old_page and "ui" or "full",
        buttons = {{
            { text = _("Refresh"), callback = function() self:showPullRequest(pr, page) end },
            { text = _("Close"), callback = function() page:onClose() end },
        }},
        link_callback = function(uri)
            local thread = uri:match("^t3:thread:(%w+)$")
            if thread then
                page:onClose()
                return UIManager:nextTick(function() self:openThread(thread) end)
            end
        end,
        close_callback = function()
            if self.pr_page == page then -- not when Refresh replaced it
                self.pr_page = nil
                self:endTyping(false)
            end
        end,
    }
    Gestures.attach(page)
    -- Hold the thread's background redraws while this is open, as the reply box does.
    self.typing = true
    self.pr_page = page
    UIManager:show(page)
    if old_page then UIManager:close(old_page) end
end

local USAGE_PERIODS = {
    { id = "today", text = _("Today") },
    { id = "week", text = _("7 days") },
    { id = "month", text = _("Month") },
}

--- Usage stats, as on T3's Usage page: cost and tokens by model and over time.
--- `period`: "today", "week" or "month". Tapping the shown period again refreshes it.
function T3:showUsage(period, old_page)
    period = period or "today"
    local ok, body = withBusy(_("Loading usage…"), function() return self.api:usage(period) end)
    if not ok then
        notify(body)
        return
    end
    local page
    local row = {}
    for _i, p in ipairs(USAGE_PERIODS) do
        table.insert(row, {
            text = p.id == period and ("• " .. p.text) or p.text,
            callback = function() self:showUsage(p.id, page) end,
        })
    end
    table.insert(row, { text = _("Close"), callback = function() page:onClose() end })
    page = ThreadView:new{
        title = str(body.title) or _("Usage"),
        html = str(body.html) or "",
        start_at_bottom = false,
        start_page = old_page and old_page.usage_period == period and old_page:currentPage() or nil,
        refresh = old_page and "ui" or "full",
        buttons = { row },
        close_callback = function()
            if self.usage_page == page then -- not when another period replaced it
                self.usage_page = nil
                self:endTyping(false)
            end
        end,
    }
    page.usage_period = period
    Gestures.attach(page)
    -- Hold the thread list's background redraws while this is open, as the PR page does.
    self.typing = true
    self.usage_page = page
    UIManager:show(page)
    if old_page then UIManager:close(old_page) end
end

-- Earlier turns loaded in place. The link at the top of a thread loads more above
-- it, and the view is then "pinned": refreshes fetch everything from its first
-- entry on (`after`), so the turns stay and page numbers keep meaning the same place.
-- Once the reader pages down to the end, the next refresh goes back to the usual
-- recent turns, so the extra ones drop off while nothing above is on screen.

--- The pin to fetch thread `id` with, or nil for the usual window.
function T3:activePin(id)
    local pin = self.pin
    if pin and pin.id == id and not self.pin_at_bottom then return pin end
end

--- Fetch a thread view, starting at `pin`'s entry if given.
function T3:fetchView(id, params, pin)
    if pin then params.after = pin.o - 1 end
    local ok, body = self.api:thread(id, params)
    if ok and pin then self:applyPin(body, pin) end
    return ok, body
end

--- A view fetched with `after` says nothing is before it; say what the pin knows.
function T3:applyPin(body, pin)
    body.more_before = pin.more_before
    body._pin = pin
end

--- The link at the top of the open thread: load the turns before it in place.
function T3:loadEarlier(view)
    if self.view ~= view or view.more_before ~= true then return end
    local first = view.entries and view.entries[1] and view.entries[1].o
    if type(first) ~= "number" then return end
    if turnCount(view) >= MAX_TURNS then return self:showOlder(view, first) end
    local expand = self:expandParam(view.id)
    local ok, body = withBusy(_("Loading…"), function()
        local ok_older, older = self.api:thread(view.id, { before = first, turns = LOAD_TURNS, expand = expand })
        if not ok_older then return false, older end
        local top = older.entries and older.entries[1] and older.entries[1].o
        if type(top) ~= "number" then return false, _("There are no earlier messages.") end
        -- Then the whole view from there, so its version matches what later polls see.
        local pin = { id = view.id, o = top, more_before = older.more_before == true }
        return self:fetchView(view.id, { progress = self:progressParam(), expand = expand }, pin)
    end)
    if not ok then
        notify(body)
        return
    end
    if self.view ~= view then return end -- closed meanwhile
    self:showThread(body, { update = true, seam = first })
end

--- Show a thread view. `opts.update`: redraw an already open thread quietly (no
--- e-ink flash), staying on the reader's page unless they were at the end.
--- `opts.page`: open on this page. `opts.seam`: open on the page just above the
--- entry with this cursor (after loading earlier turns).
function T3:showThread(view, opts)
    opts = opts or {}
    local old = self.viewer
    self.viewer = nil
    self.pin = view._pin
    if opts.seam or not opts.update then self.pin_at_bottom = false end
    local seam = opts.seam and seamIndex(view, opts.seam)
    local page = opts.page
    if not page and not opts.seam and opts.update and old and not old:atLastPage() then
        page = old:currentPage()
    end
    -- At the end of the thread, a new reply opens at its start rather than its last page.
    local anchor = not page and not opts.seam
        and anchorIndex(view, opts.update and old and self.view or nil) or nil
    local viewer
    viewer = ThreadView:new{
        title = str(view.title) or _("Thread"),
        html = composeHtml(view, self:fetchImages(view), anchor, self:pageBreakYou(), seam),
        resource_dir = imageDir(),
        buttons = self:threadButtons(view),
        start_page = page or (opts.seam and not seam and 1 or nil),
        anchor_uri = anchor and ANCHOR or nil,
        above_uri = seam and SEAM or nil,
        wide_links = { ["t3:older"] = true },
        refresh = (opts.update and old) and "ui" or "full",
        title_hold_callback = function() self:viewActions(view) end,
        scroll_callback = function(at_end)
            if self.viewer == viewer then self.pin_at_bottom = at_end end
        end,
        link_callback = function(uri)
            if uri == "t3:older" then
                return UIManager:nextTick(function() self:loadEarlier(view) end)
            end
            local run = uri:match("^t3:run:(%w+)$")
            if run then return self:toggleRun(view, run) end
            local child = uri:match("^t3:thread:(%w+)$")
            if child then return self:openChild(view, child) end
            local image = uri:match("^t3:img:(%w+)$")
            if image then return self:showImage(image) end
            local queued = uri:match("^t3:queue:(%w+)$")
            if queued then return self:queuedMessage(view, queued) end
            local pr = uri:match("^t3:pr:(%d+)$")
            if pr and type(view.prs) == "table" and view.prs[tonumber(pr)] then
                return UIManager:nextTick(function() self:showPullRequest(view.prs[tonumber(pr)]) end)
            end
        end,
        -- The title bar's X (or Back) closes the thread.
        close_callback = function()
            if self.viewer == viewer then
                self:stopThreadPoll()
                self.viewer = nil
                self.view = nil
                self.pin = nil
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
    if old then
        self:takeStackPlace(viewer, old)
        UIManager:close(old)
    end
    self:scheduleThreadPoll()
end

--- Put `new` where `old` sits in the window stack. UIManager:show puts it on top, which
--- would bury a menu or dialog that was open over the thread when an update arrived.
--- Everything above a repainted widget repaints too, so the menu stays drawn on top.
function T3:takeStackPlace(new, old)
    local stack = UIManager._window_stack
    if type(stack) ~= "table" then return end
    local old_i, new_i
    for i, window in ipairs(stack) do
        if window.widget == old then old_i = i elseif window.widget == new then new_i = i end
    end
    if not (old_i and new_i) or new_i == old_i + 1 then return end
    table.insert(stack, old_i + 1, table.remove(stack, new_i))
    -- show() applied the thread's tap settings; the widget on top keeps its own.
    local top = stack[#stack].widget
    local Input = Device.input
    if Input then
        Input.disable_double_tap = top.disable_double_tap ~= false
        Input.tap_interval_override = top.tap_interval_override
    end
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
    table.insert(self.thread_stack, { id = parent_view.id, page = self.viewer and self.viewer:currentPage(),
        pin = self:activePin(parent_view.id) })
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
    UIManager:nextTick(function() self:openThread(parent.id, parent.page, parent.pin) end)
    return true
end

function T3:reloadThread(id, page)
    local ok, view = withBusy(_("Refreshing…"), function()
        return self:fetchView(id, { progress = self:progressParam(), expand = self:expandParam(id) },
            self:activePin(id))
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
            local pin = self:activePin(view.id)
            local ok, body = self.api:changedSince(view.id, view.v, self:progressParam(),
                self:expandParam(view.id), pin and pin.o - 1 or nil)
            if self.view ~= view then return end
            if ok and pin then self:applyPin(body, pin) end
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

--- A tapped queued message: what the app's queue row offers. `item` "resume" restarts
--- a queue paused by Stop.
function T3:queuedMessage(view, item)
    local queue = type(view.queue) == "table" and view.queue or {}
    local function run(busy_text, action, which)
        local ok, err = withBusy(busy_text, function() return self.api:queueAction(view.id, action, which) end)
        if not ok then notify(err) end
        -- A message sent now lands at the end of the thread; after a removal, stay put.
        self:reloadThread(view.id, action == "remove" and self.viewer and self.viewer:currentPage() or math.huge)
    end
    if item == "resume" then
        return run(_("Resuming…"), "resume")
    end
    local text
    for _i, it in ipairs(type(queue.items) == "table" and queue.items or {}) do
        if it.id == item then text = str(it.text) end
    end
    if not text then return end
    local dialog
    local rows = {}
    local function button(label, busy_text, action)
        table.insert(rows, {{ text = label, callback = function()
            UIManager:close(dialog)
            run(busy_text, action, item)
        end }})
    end
    if queue.can_send_now == true then
        button(_("Send now"), _("Sending…"), "send")
    elseif queue.can_steer == true then
        button(_("Steer: send into the running turn"), _("Sending…"), "steer")
    end
    button(_("Remove from queue"), _("Removing…"), "remove")
    table.insert(rows, {{ text = _("Cancel"), callback = function() UIManager:close(dialog) end }})
    if #text > 300 then text = text:sub(1, 299):gsub("[\128-\191]*$", ""):gsub("[\192-\255]$", "") .. "…" end
    dialog = ButtonDialog:new{ title = text, title_align = "left", buttons = rows }
    UIManager:show(dialog)
end

--- `initial`: text to start the box with (a spoken reply from Call me).
function T3:typeReply(view, initial)
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
    local function send(text, deliver)
        local mode = plan and (plan.on and "plan" or "default") or nil
        local ok, result = withBusy(deliver == "queue" and _("Queueing…") or _("Sending…"), function()
            return self.api:reply(view.id, nil, text, self:showProgress(), self:expandList(view.id),
                mode, deliver)
        end)
        if ok then
            self:showThread(result, { update = true, page = math.huge })
        else
            notify(result)
        end
    end
    -- While the agent works, a message waits for the turn to end (Queue, as the app does
    -- by default), or goes into the running turn (Steer).
    local working = typed == "message" and str(view.state) == "working"
    self:compose{
        title = str(view.title) or _("Reply"),
        description = question,
        input = initial,
        hint = typed == "answer" and _("Your answer") or _("Message"),
        send_text = working and _("Queue") or _("Send"),
        extras = { plan and plan.button },
        alt = working and { text = _("Steer"), on_send = function(text) send(text, "steer") end } or nil,
        on_send = function(text) send(text, working and "queue" or nil) end,
    }
end

-- ---------------------------------------------------------------------------
-- voice replies

local CALL_STATUS = {
    ringing = _("Calling your phone…"),
    listening = _("Listening. Talk, then press 1 to send it, or hang up to edit it here."),
    sending = _("Sending…"),
    ending = _("Hanging up…"),
}

--- A call's state as one short line, for the call row under a thread.
callLabel = function(c)
    local status = str(c and c.status)
    if status == "ringing" or not status then return _("Ringing your phone…") end
    if status == "sending" then return _("Sending…") end
    if status == "ending" then return _("Hanging up…") end
    local parts = tonumber(c.parts) or 0
    if parts == 0 then return _("Listening…") end
    return parts == 1 and _("Listening… heard 1 part") or T(_("Listening… heard %1 parts"), parts)
end

--- Call me: the bridge rings your phone, and what you say comes back as a reply. Pressing
--- 1 on the phone sends it; hanging up opens it here in the reply box to check first.
--- No box covers the thread meanwhile: the call's status and Hang up sit on top of the
--- thread's buttons, so you can read and scroll while you talk.
function T3:callReply(view)
    self:runCall(function() return self.api:call(view.id) end, {
        thread = view.id,
        draft = function(text)
            -- Reply to the freshest view of the thread, if it's still the one open.
            local current = self.view and self.view.id == view.id and self.view or view
            self:typeReply(current, text)
        end,
        sent = function()
            notify(_("Sent."), 2)
            self:reloadThread(view.id, math.huge)
        end,
    })
end

--- Place a call with `start()` (an Api call) and show it until it ends. `on.draft(text)`:
--- the call ended with something said; `on.sent(call)`: pressing 1 sent it.
function T3:runCall(start, on)
    local ok, call = withBusy(_("Calling your phone…"), start)
    if not ok then return notify(call) end
    local cid, v = call.id, call.v
    local dialog, poll
    local over = false
    local deadline = os.time() + 15 * 60
    -- `keep_hold`: a text box opens next and takes over holding thread updates.
    -- `sent`: what was said went to T3; the caller shows the result, newer than anything held.
    local inline = on.thread ~= nil
    -- Redraw the call's thread, if it's the one on screen, to add or drop the call row.
    local function redrawThread()
        if self.view and self.view.id == on.thread and self.viewer then
            self:showThread(self.view, { update = true })
        end
    end
    local function close(keep_hold, sent)
        over = true
        UIManager:unschedule(poll)
        if dialog then UIManager:close(dialog) end
        dialog = nil
        if inline then
            if self.call and self.call.id == cid then self.call = nil end
            if not sent then redrawThread() end -- a sent reply reloads the thread anyway
        elseif not keep_hold then
            self:endTyping(sent)
        end
    end
    local finished, show -- defined below
    -- Hang up from here: the same as hanging up the phone, so what was said comes back
    -- as a draft rather than being thrown away.
    local function hangUp()
        if over then return end
        over = true -- stop polling while the bridge hangs up
        local ok, c = self.api:callCancel(cid)
        if not ok then return close() end
        if CALL_STATUS[str(c.status)] then
            -- "ending": the phone hung up, and its last words are on the way; keep waiting.
            over = false
            v = c.v
            show(c)
            UIManager:scheduleIn(1, poll)
        else
            finished(c)
        end
    end
    show = function(c)
        if inline then
            if self.call and self.call.id == cid then self.call.state = c end
            local button = self.viewer and self.viewer.button_table
                and self.viewer.button_table:getButtonById("call_status")
            if button then
                button:setText(callLabel(c), button.width)
                UIManager:setDirty(self.viewer, "ui")
            end
            return
        end
        local text = CALL_STATUS[str(c.status)] or CALL_STATUS.ringing
        local heard = str(c.text)
        if heard then text = text .. "\n\n“" .. heard .. "”" end
        if dialog then UIManager:close(dialog) end
        dialog = ButtonDialog:new{
            title = text,
            dismissable = false,
            buttons = {{{
                text = _("Hang up"),
                callback = hangUp,
            }}},
        }
        UIManager:show(dialog)
    end
    finished = function(c)
        local status = str(c.status)
        close(status == "draft", status == "sent")
        if status == "draft" then
            if str(c.error) then notify(T(_("Couldn't send it (%1). Check it and send it here."), c.error), 6) end
            on.draft(str(c.text) or "")
        elseif status == "sent" then
            on.sent(c)
        elseif status == "empty" then
            notify(_("I didn't hear anything to send."))
        elseif status == "no-answer" then
            notify(_("Your phone didn't answer."))
        elseif status == "failed" then
            notify(T(_("The call didn't go through: %1"), str(c.error) or _("no reason given")), 8)
        end
    end
    poll = function()
        if over then return end
        local got, c = self.api:callState(cid, v)
        if over then return end
        if got and c.v ~= v then
            v = c.v
            if CALL_STATUS[str(c.status)] then show(c) else return finished(c) end
        elseif os.time() > deadline then
            close()
            return notify(got and _("The call is taking too long; giving up on it.") or c)
        end
        UIManager:scheduleIn(2, poll)
    end
    if inline then
        self.call = { id = cid, thread = on.thread, state = call, hang_up = hangUp }
        redrawThread()
        notify(_("Press 1 on the phone to send, or hang up to check it here first."), 3)
    else
        -- Like a text box: hold background redraws (a working thread's next step) so they
        -- don't cover the call; they show once it ends.
        self.typing = true
        show(call)
    end
    UIManager:scheduleIn(2, poll)
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
--- in one request and isn't kept anywhere on the device. A card with fields (say, email
--- and password) asks for each in turn, showing what's typed only for fields that aren't
--- secret, and sends them together. Cancel or a failed save never declines the request.
function T3:enterSecret(view, step, values)
    local p = type(view.pending) == "table" and view.pending or {}
    local fields = type(p.fields) == "table" and #p.fields > 0 and p.fields or nil
    step, values = step or 1, values or {}
    local field = fields and fields[step] or {}
    local last = not fields or step == #fields
    local description = str(p.text)
    if fields then
        local label = str(field.label) or str(field.name) or ""
        if #fields > 1 then label = T(_("%1 (%2 of %3)"), label, step, #fields) end
        description = description and (description .. "\n\n" .. label) or label
    end
    local dialog
    dialog = InputDialog:new{
        title = str(p.header) or _("Secret"),
        description = description,
        input = "",
        input_hint = (fields and (str(field.placeholder) or str(field.label)))
            or str(p.placeholder) or _("Secret"),
        text_type = (not fields or field.secret ~= false) and "password" or nil,
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            {
                text = last and _("Save") or _("Next"),
                is_enter_default = true,
                callback = function()
                    local secret = dialog:getInputText()
                    if not secret:match("%S") then return end
                    dialog._t3_sent = true
                    UIManager:close(dialog)
                    if fields then
                        values[str(field.name)] = secret
                        secret = nil
                        if not last then return self:enterSecret(view, step + 1, values) end
                    end
                    local ok, result = withBusy(_("Saving…"), function()
                        return self.api:answerSecret(view.id, secret, fields and values or nil)
                    end)
                    secret, values = nil, nil
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
    -- o.alt: a second way to send ({text, on_send}), left of the main one.
    if o.alt then
        table.insert(row, {
            text = o.alt.text,
            callback = function()
                local text = dialog:getInputText()
                if not text:match("%S") then return end
                dialog._t3_sent = true
                UIManager:close(dialog)
                o.alt.on_send(text)
            end,
        })
    end
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
        wide_links = { ["t3:older"] = true },
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
    self.voice_on = body.call == true
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
    dialog = ButtonDialog:new{ buttons = rows }
    -- The title goes in a title bar so there's an X in the corner to close the list.
    dialog:addWidget(TitleBar:new{
        width = dialog:getAddedWidgetAvailableWidth(),
        align = "left",
        title = _("New thread in…"),
        button_padding = 0,
        close_callback = function() UIManager:close(dialog) end,
        show_parent = dialog,
        not_focusable = true,
    })
    UIManager:show(dialog)
end

--- `state`: when coming back from Call me, what the box had: {text, model, model_label, plan}.
function T3:newThread(project, state)
    state = state or {}
    local model = state.model -- nil: the project's default model, from T3's settings
    local model_label = state.model_label
    local extra
    extra = {
        text = model_label or _("Model"),
        id = "model",
        callback = function()
            -- Put the keyboard away first: while it's up, the first tap on the
            -- picker only closes it.
            local dialog = self.compose_dialog
            if dialog and dialog:isKeyboardVisible() then dialog:onCloseKeyboard() end
            self:pickModel({ project = project.id }, function(id, label)
                model, model_label = id, label
                -- Show the choice on the button.
                self:setComposeButton(extra, label or _("Model"))
                dialog = self.compose_dialog
                if dialog then dialog:onShowKeyboard() end
            end)
        end,
    }
    local plan = self:planToggle(state.plan == true)
    local extras = { extra, plan.button }
    if self.voice_on then
        table.insert(extras, {
            text = _("Call me"),
            callback = function()
                local dialog = self.compose_dialog
                local typed = dialog and dialog:getInputText() or ""
                if dialog then UIManager:close(dialog) end
                local mode = plan.on and "plan" or nil
                self:runCall(function() return self.api:callNew(project.id, model, mode) end, {
                    draft = function(text)
                        -- Anything typed before the call stays, with the spoken text after it.
                        if typed:match("%S") then text = typed:gsub("%s+$", "") .. " " .. text end
                        self:newThread(project, { text = text, model = model, model_label = model_label,
                            plan = plan.on })
                    end,
                    sent = function(c)
                        local id = str(c.thread)
                        if not id then return notify(_("Started.")) end
                        self:openThread(id)
                    end,
                })
            end,
        })
    end
    self:compose{
        title = T(_("New thread in %1"), str(project.title) or ""),
        input = state.text,
        hint = _("What should the agent do?"),
        send_text = _("Start"),
        extras = extras,
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
