--[[--
Lets KOReader's own gestures work over the plugin's full-screen views.

UIManager only hands touch input to the top widget, so while a thread or
the thread list is open, the reader's (or file browser's) gesture shortcuts
and its top menu are unreachable. attach() makes a widget pass gestures on
to them:

- swipes, multiswipes and two-finger gestures go to the user's gesture
  shortcuts and the top menu first, as they do in the reader, so an edge
  swipe for brightness wins over scrolling the page;
- taps and holds go there only when the view itself ignored them, so
  buttons, links and list items keep working.

Only zones from the Gestures plugin and the top menu are tried, never the
reader's own page-turn or highlight zones. Actions those gestures trigger
(e.g. brightness) are sent to the top widget, so while one runs, events
the view doesn't handle are passed down to the reader or file browser.
--]]

local M = {}

local MENU_ZONES = {
    readermenu_tap = true, readermenu_ext_tap = true,
    readermenu_swipe = true, readermenu_ext_swipe = true,
    filemanager_tap = true, filemanager_ext_tap = true,
    filemanager_swipe = true, filemanager_ext_swipe = true,
}

-- Gesture types tried before the view itself; the rest only as a fallback.
local EARLY = {
    swipe = true, multiswipe = true,
    two_finger_tap = true, two_finger_swipe = true,
    spread = true, pinch = true, rotate = true,
}
local LATE = { tap = true, hold = true, double_tap = true }

local function currentUI()
    local ReaderUI = package.loaded["apps/reader/readerui"]
    if ReaderUI and ReaderUI.instance then return ReaderUI.instance end
    local FileManager = package.loaded["apps/filemanager/filemanager"]
    return FileManager and FileManager.instance
end

-- Touch zones worth forwarding, in the order KOReader itself would try them.
local function zones(ui)
    local configured = ui.gestures and ui.gestures.gestures or {}
    local list = {}
    local function add(container)
        if type(container) ~= "table" or type(container._ordered_touch_zones) ~= "table" then return end
        for _, tz in ipairs(container._ordered_touch_zones) do
            local id = tz.def and tz.def.id
            if id and (MENU_ZONES[id] or configured[id] ~= nil or id == "multiswipe") then
                table.insert(list, tz)
            end
        end
    end
    -- The file browser keeps its menu zones on the menu module, which sees
    -- gestures before the file browser itself; the reader keeps both on itself.
    add(ui.menu)
    add(ui)
    return list
end

local function forward(widget, ges, types)
    local ui = currentUI()
    if not ui or type(ges) ~= "table" or not types[ges.ges] then return false end
    for _, tz in ipairs(zones(ui)) do
        if tz.gs_range:match(ges) then
            widget._t3_forward_to = ui
            local ok, handled = pcall(tz.handler, ges)
            widget._t3_forward_to = nil
            if ok and handled then return true end
        end
    end
    return false
end

function M.attach(widget)
    local handleEvent = widget.handleEvent
    widget.handleEvent = function(self, event)
        if event.handler == "onGesture" then
            local ges = event.args and event.args[1]
            if forward(self, ges, EARLY) then return true end
            if handleEvent(self, event) then return true end
            return forward(self, ges, LATE)
        end
        local handled = handleEvent(self, event)
        if not handled and self._t3_forward_to and self._t3_forward_to ~= self then
            return self._t3_forward_to:handleEvent(event)
        end
        return handled
    end
    return widget
end

return M
