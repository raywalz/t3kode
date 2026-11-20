--[[--
Full-screen thread view: a title bar, the conversation rendered as HTML
(MuPDF, like KOReader's dictionary popups), and a table of reply buttons.

Scrolling is page by page, which suits e-ink: swipe up/down, or tap the
right/left half of the text.
--]]

local Blitbuffer = require("ffi/blitbuffer")
local ButtonTable = require("ui/widget/buttontable")
local Device = require("device")
local FrameContainer = require("ui/widget/container/framecontainer")
local InputContainer = require("ui/widget/container/inputcontainer")
local ScrollHtmlWidget = require("ui/widget/scrollhtmlwidget")
local Size = require("ui/size")
local TitleBar = require("ui/widget/titlebar")
local UIManager = require("ui/uimanager")
local VerticalGroup = require("ui/widget/verticalgroup")
local Screen = Device.screen

-- MuPDF's CSS parser rejects < and > even inside comments.
local CSS = [[
@page { margin: 0 0 0 0; font-family: 'Noto Sans'; }
body { margin: 0; padding: 0; line-height: 1.3; text-align: left; }
p { margin: 0 0 0.45em 0; }
h3, h4, h5, h6 { margin: 0.6em 0 0.25em 0; font-size: 1em; font-weight: bold; }
h3 { font-size: 1.1em; }
ul, ol { margin: 0 0 0.45em 0; padding-left: 1.3em; }
li { margin: 0 0 0.15em 0; }
pre { margin: 0.3em 0 0.5em 0; padding: 0.3em; font-family: monospace; font-size: 0.82em;
      white-space: pre-wrap; border: 1px solid #888; }
code { font-family: monospace; font-size: 1em; }
blockquote { margin: 0 0 0.45em 0.6em; padding-left: 0.6em; border-left: 3px solid #888; }
table { border-collapse: collapse; margin: 0.2em 0 0.5em 0; font-size: 0.9em; }
th, td { border: 1px solid #888; padding: 0.15em 0.35em; text-align: left; vertical-align: top; }
hr { border: 0; border-top: 1px solid #888; margin: 0.5em 0; }
.entry { margin: 0 0 0.9em 0; }
.you { border-left: 4px solid black; padding-left: 0.5em; }
.label { font-weight: bold; font-size: 0.8em; margin-bottom: 0.15em; }
.tool, .note { color: #444; font-size: 0.85em; }
.tool p, .note p { margin: 0; }
.status { color: #444; font-style: italic; font-size: 0.9em; margin-top: 0.4em; }
.pending { border: 2px solid black; padding: 0.45em; margin-top: 0.6em; }
.pending-title { font-weight: bold; margin-bottom: 0.25em; }
.more { color: #444; font-size: 0.85em; text-align: center; margin-bottom: 0.8em; }
a { color: black; text-decoration: none; }
.fold { border: 1px solid #888; padding: 0.25em 0.5em; color: #333; font-size: 0.9em; }
.fold p { margin: 0; }
]]

local ThreadView = InputContainer:extend{
    title = nil,
    html = "",
    buttons = nil, -- ButtonTable rows
    font_size = 20,
    start_at_bottom = true,
    start_page = nil, -- open on this page instead (e.g. to stay put after expanding a turn)
    refresh = "full", -- e-ink refresh on show: "full" flashes to clear ghosting, "ui" doesn't
    close_callback = nil,
    link_callback = nil, -- function(uri) for taps on links in the HTML
    covers_fullscreen = true,
}

function ThreadView:init()
    self.width = Screen:getWidth()
    self.height = Screen:getHeight()
    self.dimen = { x = 0, y = 0, w = self.width, h = self.height }
    local pad = Size.padding.large

    self.titlebar = TitleBar:new{
        width = self.width,
        align = "left",
        with_bottom_line = true,
        title = self.title,
        title_shrink_font_to_fit = true,
        close_callback = function() self:onClose() end,
        show_parent = self,
    }
    self.button_table = ButtonTable:new{
        width = self.width - 2 * Size.padding.default,
        buttons = self.buttons or {},
        zero_sep = true,
        show_parent = self,
    }
    local html_height = self.height - self.titlebar:getHeight() - self.button_table:getSize().h - 2 * pad
    self.html_widget = ScrollHtmlWidget:new{
        html_body = self.html,
        css = CSS,
        default_font_size = Screen:scaleBySize(self.font_size),
        width = self.width - 2 * pad,
        height = html_height,
        dialog = self,
        html_link_tapped_callback = self.link_callback and function(link)
            if type(link) == "table" and type(link.uri) == "string" then self.link_callback(link.uri) end
        end or nil,
    }
    local box = self.html_widget.htmlbox_widget
    if self.start_page and self.start_page > 1 then
        box:setPageNumber(math.min(self.start_page, box.page_count))
        self.html_widget:_updateScrollBar()
        box:freeBb()
        box:_render()
    elseif self.start_at_bottom and not self.start_page then
        self.html_widget:scrollToRatio(1)
    end

    self[1] = FrameContainer:new{
        background = Blitbuffer.COLOR_WHITE,
        bordersize = 0,
        padding = 0,
        width = self.width,
        height = self.height,
        VerticalGroup:new{
            align = "left",
            self.titlebar,
            FrameContainer:new{
                bordersize = 0,
                padding = pad,
                self.html_widget,
            },
            FrameContainer:new{
                bordersize = 0,
                padding = 0,
                padding_left = Size.padding.default,
                self.button_table,
            },
        },
    }

    if Device:hasKeys() then
        self.key_events = { Close = { { Device.input.group.Back } } }
    end
end

function ThreadView:currentPage()
    return self.html_widget.htmlbox_widget.page_number
end

function ThreadView:atLastPage()
    local box = self.html_widget.htmlbox_widget
    return box.page_number >= box.page_count
end

function ThreadView:onShow()
    -- Opening a thread flashes once to clear ghosting; updates to an open thread don't.
    UIManager:setDirty(self, self.refresh)
    return true
end

function ThreadView:onClose()
    UIManager:close(self)
    if self.close_callback then self.close_callback() end
    return true
end

function ThreadView:onCloseWidget()
    UIManager:setDirty(nil, "ui")
end

return ThreadView
