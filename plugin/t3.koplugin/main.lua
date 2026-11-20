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
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local logger = require("logger")
local _ = require("gettext")
local T = require("ffi/util").template

local Api = require("t3_api")
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
    self.ui.menu:registerToMainMenu(self)
end

function T3:makeApi()
    self.api = Api.new(self.settings:readSetting("bridge_url"), self.settings:readSetting("bridge_token"))
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
    menu_items.t3_settings = {
        text = _("T3 Code settings"),
        sorting_hint = "more_tools",
        sub_item_table = {
            {
                text = _("Show progress while working"),
                help_text = _("Redraw an open thread as the agent works, not only when its turn finishes."),
                checked_func = function() return self:showProgress() end,
                callback = function()
                    self.settings:saveSetting("show_progress", not self:showProgress())
                    self.settings:flush()
                end,
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

function T3:showProgress()
    return self.settings:nilOrTrue("show_progress")
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
        local header = str(p.title) or _("No project")
        if not project_filter then
            -- What tapping the header reveals: more active threads, and the settled ones.
            local extra = {}
            local more = tonumber(p.more) or 0
            local settled = tonumber(p.settled) or 0
            if more > 0 then table.insert(extra, T(_("%1 more"), more)) end
            if settled > 0 then table.insert(extra, T(_("%1 settled"), settled)) end
            if #extra > 0 then header = header .. "  (" .. table.concat(extra, " · ") .. ")" end
        end
        table.insert(items, {
            text = header,
            bold = true,
            callback = function()
                if not project_filter and str(p.id) then self:showList(p.id, str(p.title)) end
            end,
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
    menu.onMenuHold = function(_menu, item)
        if item.hold_callback then item.hold_callback() end
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
        if not self.viewer and not self.typing and NetworkMgr:isConnected() then
            pcall(function() self:refreshList(false) end)
        end
        self:scheduleListPoll()
    end
    UIManager:scheduleIn(POLL_LIST, self.list_poll)
end

--- Hold on a thread in the list: settle it (or bring it back).
function T3:threadActions(t)
    local settled = t.settled == true
    local dialog
    dialog = ButtonDialog:new{
        title = str(t.title) or _("Thread"),
        buttons = {
            {{
                text = settled and _("Unsettle") or _("Settle"),
                callback = function()
                    UIManager:close(dialog)
                    local ok, err = withBusy(settled and _("Unsettling…") or _("Settling…"), function()
                        return self.api:settle(t.id, not settled)
                    end)
                    if not ok then
                        notify(err)
                        return
                    end
                    pcall(function() self:refreshList(true) end)
                end,
            }},
            {{ text = _("Cancel"), callback = function() UIManager:close(dialog) end }},
        },
    }
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
        },
    }
    UIManager:show(dialog)
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
    local width = Device.screen:getWidth()
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
            local tmp = dir .. "/" .. id .. ".download"
            local ok, ctype = self.api:download("/v1/images/" .. id, { w = width }, tmp)
            local ext = ok and EXT_FOR_TYPE[(tostring(ctype or "")):match("^[^;%s]+")]
            if ext then
                os.rename(tmp, dir .. "/" .. id .. "." .. ext)
                have[id] = id .. "." .. ext
            else
                os.remove(tmp)
                logger.warn("t3: image", id, ok and ("unsupported type " .. tostring(ctype)) or ctype)
                self.image_failed[id] = true -- don't retry on every redraw
            end
        end
    end)
    return have
end

function T3:showImage(id)
    local dir = imageDir()
    local name = cachedImage(dir, id)
    if not name then return end
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

local function composeHtml(view, have)
    local parts = {}
    if view.more_before == true then
        table.insert(parts, '<div class="more">' .. esc(_("⋯ Earlier messages: tap Older.")) .. "</div>")
    end
    for _i, e in ipairs(view.entries or {}) do
        local role = str(e.role) or "note"
        local body = str(e.html) or ("<p>" .. esc(str(e.text)) .. "</p>")
        body = placeImages(body, have or {})
        local label = ROLE_LABEL[role]
        if role == "fold" then label = nil end
        table.insert(parts, '<div class="entry ' .. role .. '">'
            .. (label and ('<div class="label">' .. esc(label) .. "</div>") or "")
            .. body .. "</div>")
    end
    local state = str(view.state)
    if state == "working" then
        table.insert(parts, '<div class="status">' .. esc(_("Working…")) .. "</div>")
    elseif state == "failed" then
        table.insert(parts, '<div class="status">' .. esc(_("The last run failed.")) .. "</div>")
    end
    local p = view.pending
    if type(p) == "table" then
        local lines = { '<div class="pending"><div class="pending-title">' .. esc(_("Needs you")) .. "</div>" }
        local head = str(p.header)
        local step = str(p.step)
        table.insert(lines, "<p>" .. (head and ("<b>" .. esc(head) .. ":</b> ") or "") .. esc(str(p.text))
            .. (step and (" (" .. esc(step) .. ")") or "") .. "</p>")
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

function T3:threadButtons(view)
    local rows = {}
    local shortcuts = {}
    local answers = {}
    for _i, r in ipairs(view.replies or {}) do
        local id = str(r.id)
        local button = {
            text = SHORT_LABELS[id] or str(r.label) or id,
            enabled = r.enabled ~= false,
            callback = function() self:sendReply(view, id, r.kind == "toggle") end,
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
    local viewer
    viewer = ThreadView:new{
        title = str(view.title) or _("Thread"),
        html = composeHtml(view, self:fetchImages(view)),
        resource_dir = imageDir(),
        buttons = self:threadButtons(view),
        start_page = page,
        refresh = (opts.update and old) and "ui" or "full",
        link_callback = function(uri)
            local run = uri:match("^t3:run:(%w+)$")
            if run then return self:toggleRun(view, run) end
            local child = uri:match("^t3:thread:(%w+)$")
            if child then return self:openChild(view, child) end
            local image = uri:match("^t3:img:(%w+)$")
            if image then return self:showImage(image) end
        end,
        close_callback = function()
            if self.viewer == viewer then
                self:stopThreadPoll()
                self.viewer = nil
                self.view = nil
                self:returnToParent(view)
            end
        end,
    }
    self.viewer = viewer
    self.view = view
    -- Show the new view before closing the old one, so nothing underneath flashes through.
    UIManager:show(viewer)
    if old then UIManager:close(old) end
    self:scheduleThreadPoll()
end

function T3:closeThread()
    self:stopThreadPoll()
    local viewer = self.viewer
    local view = self.view
    self.viewer = nil
    self.view = nil
    if viewer then UIManager:close(viewer) end
    if view and self:returnToParent(view) then return end
    -- Pick up whatever changed while the thread was open.
    pcall(function() self:refreshList(false) end)
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
    local title = _("Reply")
    if typed == "answer" and p then title = str(p.text) or _("Answer") end
    self:compose{
        title = title,
        hint = typed == "answer" and _("Your answer") or _("Message"),
        send_text = _("Send"),
        on_send = function(text)
            local ok, result = withBusy(_("Sending…"), function()
                return self.api:reply(view.id, nil, text, self:showProgress(), self:expandList(view.id))
            end)
            if ok then
                self:showThread(result, { update = true, page = math.huge })
            else
                notify(result)
            end
        end,
    }
end

--- A text box that grows with its text (up to the space above the keyboard, then it
--- scrolls). While it's open, background checks don't redraw anything over it.
local COMPOSE_MIN_LINES = 4

function T3:compose(o)
    local dialog
    local function textBox()
        local w = dialog._input_widget and dialog._input_widget.text_widget
        local inner = w and (w.text_widget or w)
        if inner and inner.getAllLineCount and inner.getVisLineCount and inner.getLineHeight then
            return inner
        end
    end
    -- KOReader sizes the box only when it's built, so resizing means rebuilding it.
    -- Like InputDialog:reinit(), minus its full-screen flash. `height` nil: fit the text.
    local function rebuild(height, quiet)
        dialog._t3_rebuilding = true
        -- Before it's first shown, lay it out for the keyboard that's about to appear.
        local visible = quiet or dialog:isKeyboardVisible()
        dialog.input = dialog:getInputText()
        dialog:onClose() -- remembers the cursor position for init()
        dialog._input_widget:onCloseWidget()
        dialog:free()
        dialog.text_height = height
        dialog.keyboard_visible = visible and true or false
        dialog:init()
        if dialog.keyboard_visible and not quiet then dialog:onShowKeyboard() end
        dialog._t3_rebuilding = nil
        if not quiet then UIManager:setDirty("all", "ui") end
    end
    local function minHeight()
        local box = textBox()
        return box and COMPOSE_MIN_LINES * box:getLineHeight()
    end
    dialog = InputDialog:new{
        title = o.title,
        input = "",
        input_hint = o.hint,
        allow_newline = true,
        strike_callback = function()
            if not dialog or dialog._t3_rebuilding or dialog._t3_check_queued then return end
            dialog._t3_check_queued = true
            UIManager:nextTick(function()
                dialog._t3_check_queued = nil
                local box = textBox()
                if not box then return end
                local all, visible = box:getAllLineCount(), box:getVisLineCount()
                if all <= COMPOSE_MIN_LINES then
                    -- Short text keeps the fixed four-line box: no resizing, no redraws.
                    if visible ~= COMPOSE_MIN_LINES then
                        dialog._t3_full = nil
                        rebuild(minHeight())
                    end
                elseif all > visible and not dialog._t3_full then
                    rebuild(nil)
                    box = textBox()
                    -- Still more lines than fit: it's as tall as the screen allows; scroll now.
                    dialog._t3_full = box and box:getAllLineCount() > box:getVisLineCount() or nil
                elseif all < visible then
                    dialog._t3_full = nil
                    rebuild(nil) -- shrink back after deleting lines
                end
            end)
        end,
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            {
                text = o.send_text,
                is_enter_default = true,
                callback = function()
                    local text = dialog:getInputText()
                    if not text:match("%S") then return end
                    UIManager:close(dialog)
                    o.on_send(text)
                end,
            },
        }},
    }
    dialog.onCloseWidget = function(d)
        InputDialog.onCloseWidget(d)
        self:endTyping()
    end
    -- Start at four lines (sized before it's shown, so it costs no extra redraw).
    rebuild(minHeight(), true)
    self.typing = true
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

--- The text box closed: show any thread update that arrived while it was open.
function T3:endTyping()
    self.typing = false
    local held = self.held_view
    self.held_view = nil
    if held and self.view and held.id == self.view.id then
        UIManager:nextTick(function()
            if self.view and held.id == self.view.id and not self.typing then
                self:showThread(held, { update = true })
            end
        end)
    end
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
        html = composeHtml(older, self:fetchImages(older)),
        resource_dir = imageDir(),
        buttons = {{
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
    self:compose{
        title = T(_("New thread in %1"), str(project.title) or ""),
        hint = _("What should the agent do?"),
        send_text = _("Start"),
        on_send = function(text)
            local ok, view = withBusy(_("Starting…"), function()
                return self.api:startThread(project.id, text)
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
