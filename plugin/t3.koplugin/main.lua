--[[--
T3 Code client for KOReader.

Shows T3 Code threads from a t3-koreader bridge and lets you reply with a tap.
All T3-specific work happens on the bridge; this plugin renders what it gets.

@module koplugin.t3
--]]

local ButtonDialog = require("ui/widget/buttondialog")
local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local Dispatcher = require("dispatcher")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local NetworkMgr = require("ui/network/manager")
local TextViewer = require("ui/widget/textviewer")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local logger = require("logger")
local _ = require("gettext")
local T = require("ffi/util").template

local Api = require("t3_api")

local LIST_LIMIT = 8
local POLL_ACTIVE = 15 -- seconds, while a thread is working or waiting for you
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
    self.ui.menu:registerToMainMenu(self)
end

function T3:makeApi()
    self.api = Api.new(self.settings:readSetting("bridge_url"), self.settings:readSetting("bridge_token"))
end

function T3:addToMainMenu(menu_items)
    menu_items.t3 = {
        text = _("T3 Code"),
        sorting_hint = "more_tools",
        sub_item_table = {
            {
                text = _("Threads"),
                callback = function() self:onT3Open() end,
            },
            {
                text = _("Bridge address"),
                keep_menu_open = true,
                callback = function() self:editSetting("bridge_url", _("Bridge address"),
                    "http://100.64.0.1:8787", false) end,
            },
            {
                text = _("Bridge token"),
                keep_menu_open = true,
                callback = function() self:editSetting("bridge_token", _("Bridge token"), "", true) end,
            },
        },
    }
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

--- Run fn while showing a short "working" message, so a slow call doesn't look like a freeze.
local function withBusy(text, fn)
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

local function str(v)
    return type(v) == "string" and v or nil
end

-- ---------------------------------------------------------------------------
-- thread list

function T3:onT3Open()
    NetworkMgr:runWhenConnected(function() self:showList() end)
    return true
end

function T3:listItems(body, project_filter)
    local items = {}
    for _i, p in ipairs(body.projects or {}) do
        local shown = #(p.threads or {})
        local header = str(p.title) or _("No project")
        if not project_filter and type(p.total) == "number" and p.total > shown then
            header = T(_("%1  (%2 more)"), header, p.total - shown)
        end
        table.insert(items, {
            text = header,
            bold = true,
            callback = function()
                if not project_filter and str(p.id) then self:showList(p.id, header) end
            end,
        })
        for _j, t in ipairs(p.threads or {}) do
            local state = str(t.state) or "done"
            local label = STATE_LABEL[state] or state
            if state == "done" then
                label = (t.unread == true and _("new") .. " · " or "") .. ago(t.updated)
            end
            table.insert(items, {
                text = "    " .. (str(t.title) or _("Untitled")),
                mandatory = label,
                bold = state == "needs_you" or t.unread == true,
                callback = function() self:openThread(t.id) end,
            })
        end
    end
    if #items == 0 then
        table.insert(items, { text = _("No threads yet.") })
    end
    return items
end

function T3:showList(project_id, title)
    local params = { limit = project_id and 200 or LIST_LIMIT, project = project_id }
    local ok, body = withBusy(_("Loading threads…"), function()
        return self.api:request("GET", "/v1/threads", params)
    end)
    if not ok then
        notify(body)
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
            if self.list and self.list.menu == menu then self.list = self.list.parent end
        end,
    }
    -- Keep the list open underneath a thread instead of closing on tap.
    menu.onMenuSelect = function(_menu, item)
        if item.callback then item.callback() end
        return true
    end
    menu.onLeftButtonTap = function() self:showListActions() end
    self.list = { menu = menu, v = body.v, params = params, project_id = project_id, parent = self.list }
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

function T3:scheduleListPoll()
    if self.list_poll then UIManager:unschedule(self.list_poll) end
    self.list_poll = function()
        self.list_poll = nil
        if not self.list then return end
        -- Don't redraw the list while a thread is open on top of it.
        if not self.viewer and NetworkMgr:isConnected() then
            pcall(function() self:refreshList(false) end)
        end
        self:scheduleListPoll()
    end
    UIManager:scheduleIn(POLL_LIST, self.list_poll)
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
        },
    }
    UIManager:show(dialog)
end

-- ---------------------------------------------------------------------------
-- thread view

function T3:openThread(id)
    local ok, view = withBusy(_("Loading…"), function()
        return self.api:thread(id, { visit = 1 })
    end)
    if not ok then
        notify(view)
        return
    end
    self:showThread(view)
end

local function composeText(view)
    local parts = {}
    if view.more_before == true then
        table.insert(parts, _("⋯ Earlier messages: tap Older."))
    end
    for _i, e in ipairs(view.entries or {}) do
        local text = str(e.text) or ""
        if e.role == "you" then
            table.insert(parts, _("You: ") .. text)
        elseif e.role == "note" then
            table.insert(parts, "• " .. text)
        else
            table.insert(parts, text)
        end
    end
    local state = str(view.state)
    if state == "working" then
        table.insert(parts, _("… Working. This view updates when the turn finishes."))
    elseif state == "failed" then
        table.insert(parts, _("The last run failed."))
    end
    local p = view.pending
    if type(p) == "table" then
        local lines = { _("── Needs you ──") }
        local head = str(p.header)
        local step = str(p.step)
        table.insert(lines, (head and (head .. ": ") or "") .. (str(p.text) or "")
            .. (step and ("  (" .. step .. ")") or ""))
        if str(p.note) then table.insert(lines, str(p.note)) end
        for _i, o in ipairs(type(p.options) == "table" and p.options or {}) do
            local desc = str(o.description)
            table.insert(lines, "• " .. (str(o.label) or "") .. (desc and (": " .. desc) or ""))
        end
        table.insert(parts, table.concat(lines, "\n"))
    end
    if #parts == 0 then
        return _("Nothing here yet.")
    end
    return table.concat(parts, "\n\n")
end

local SHORT_LABELS = { ["c:summarize"] = _("Summarize"), ["c:continue"] = _("Continue"), ["c:stop"] = _("Stop") }

function T3:threadButtons(view)
    local rows = {}
    local shortcuts = {}
    local answers = {}
    for _i, r in ipairs(view.replies or {}) do
        local id = str(r.id)
        local button = {
            text = SHORT_LABELS[id] or str(r.label) or id,
            callback = function() self:sendReply(view, id) end,
        }
        if r.kind == "shortcut" then
            table.insert(shortcuts, button)
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
            text = _("Type…"),
            enabled = typed ~= "none",
            callback = function() self:typeReply(view) end,
        },
        {
            text = _("Older"),
            enabled = view.more_before == true,
            callback = function() self:showOlder(view, view.entries and view.entries[1] and view.entries[1].o) end,
        },
        {
            text = _("Refresh"),
            callback = function() self:reloadThread(view.id) end,
        },
        {
            text = _("Close"),
            callback = function() self:closeThread() end,
        },
    })
    return rows
end

function T3:showThread(view)
    local old = self.viewer
    self.viewer = nil
    if old then UIManager:close(old) end
    local viewer
    viewer = TextViewer:new{
        title = str(view.title) or _("Thread"),
        text = composeText(view),
        buttons_table = self:threadButtons(view),
        show_menu = false,
        justified = false,
        auto_para_direction = false,
        close_callback = function()
            if self.viewer == viewer then self:stopThreadPoll(); self.viewer = nil end
        end,
    }
    -- Newest messages are at the bottom.
    if viewer.scroll_text_w then viewer.scroll_text_w:scrollToBottom() end
    self.viewer = viewer
    self.view = view
    UIManager:show(viewer)
    self:scheduleThreadPoll()
end

function T3:closeThread()
    self:stopThreadPoll()
    local viewer = self.viewer
    self.viewer = nil
    self.view = nil
    if viewer then UIManager:close(viewer) end
    -- Pick up whatever changed while the thread was open.
    pcall(function() self:refreshList(false) end)
end

function T3:reloadThread(id)
    local ok, view = withBusy(_("Refreshing…"), function() return self.api:thread(id) end)
    if not ok then
        notify(view)
        return
    end
    self:showThread(view)
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
    self.thread_poll = function()
        self.thread_poll = nil
        if self.view ~= view then return end
        if NetworkMgr:isConnected() then
            local ok, body = self.api:changedSince(view.id, view.v)
            if self.view ~= view then return end
            if ok and body.changed == true then
                self:showThread(body)
                return
            end
        end
        self:scheduleThreadPoll()
    end
    UIManager:scheduleIn(busy and POLL_ACTIVE or POLL_IDLE, self.thread_poll)
end

function T3:sendReply(view, reply_id)
    local function go()
        local ok, result = withBusy(_("Sending…"), function()
            return self.api:reply(view.id, reply_id, nil)
        end)
        if ok then
            self:showThread(result)
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
    local title = _("Reply")
    if typed == "answer" and p then title = str(p.text) or _("Answer") end
    local dialog
    dialog = InputDialog:new{
        title = title,
        input = "",
        input_hint = typed == "answer" and _("Your answer") or _("Message"),
        allow_newline = true,
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            {
                text = _("Send"),
                is_enter_default = true,
                callback = function()
                    local text = dialog:getInputText()
                    if not text:match("%S") then return end
                    UIManager:close(dialog)
                    local ok, result = withBusy(_("Sending…"), function()
                        return self.api:reply(view.id, nil, text)
                    end)
                    if ok then
                        self:showThread(result)
                    else
                        notify(result)
                    end
                end,
            },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function T3:showOlder(view, before)
    if type(before) ~= "number" then return end
    local ok, older = withBusy(_("Loading…"), function()
        return self.api:thread(view.id, { before = before, turns = 3 })
    end)
    if not ok then
        notify(older)
        return
    end
    older.state = "done" -- an archive page: no live status line
    older.pending = nil
    local first = older.entries and older.entries[1] and older.entries[1].o
    local older_viewer
    older_viewer = TextViewer:new{
        title = T(_("%1 (earlier)"), str(view.title) or _("Thread")),
        text = composeText(older),
        show_menu = false,
        justified = false,
        auto_para_direction = false,
        buttons_table = {{
            {
                text = _("Older"),
                enabled = older.more_before == true,
                callback = function()
                    UIManager:close(older_viewer)
                    self:showOlder(view, first)
                end,
            },
            { text = _("Close"), callback = function() UIManager:close(older_viewer) end },
        }},
    }
    if older_viewer.scroll_text_w then older_viewer.scroll_text_w:scrollToBottom() end
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
    local dialog
    dialog = InputDialog:new{
        title = T(_("New thread in %1"), str(project.title) or ""),
        input = "",
        input_hint = _("What should the agent do?"),
        allow_newline = true,
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            {
                text = _("Start"),
                is_enter_default = true,
                callback = function()
                    local text = dialog:getInputText()
                    if not text:match("%S") then return end
                    UIManager:close(dialog)
                    local ok, view = withBusy(_("Starting…"), function()
                        return self.api:startThread(project.id, text)
                    end)
                    if ok then
                        self:showThread(view)
                    else
                        notify(view)
                    end
                end,
            },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function T3:onCloseWidget()
    self:stopThreadPoll()
    if self.list_poll then UIManager:unschedule(self.list_poll) end
end

return T3
