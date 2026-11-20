--[[--
HTTP client for the t3-koreader bridge (docs/bridge-api.md).

Calls are synchronous: KOReader's UI runs on one thread, so every call here
blocks the screen until it returns. Keep timeouts short and never long-poll.
--]]

local JSON = require("json")
local http = require("socket.http")
local ltn12 = require("ltn12")
local socket = require("socket")
local socketutil = require("socketutil")
local logger = require("logger")

local Api = {}
Api.__index = Api

-- KOReader's JSON decoder turns null into a function, so test values by type.
function Api.isNull(v)
    return v == nil or type(v) == "function"
end

function Api.new(base_url, token)
    local self = setmetatable({}, Api)
    self.base_url = (base_url or ""):gsub("/+$", "")
    self.token = token or ""
    return self
end

function Api:configured()
    return self.base_url ~= "" and self.token ~= ""
end

local function urlencode(s)
    return (tostring(s):gsub("[^%w%-_%.~]", function(c) return string.format("%%%02X", c:byte()) end))
end

local function query(params)
    if not params then return "" end
    local parts = {}
    for k, v in pairs(params) do
        if v ~= nil then
            table.insert(parts, urlencode(k) .. "=" .. urlencode(v))
        end
    end
    if #parts == 0 then return "" end
    return "?" .. table.concat(parts, "&")
end

-- A unique id per tap. Reusing it on retry lets the bridge drop the duplicate.
local counter = 0
function Api.newMessageId()
    counter = counter + 1
    return string.format("%x-%x-%x", os.time(), math.random(0, 0x3fffffff), counter)
end

--- Returns ok, body_or_error_message, http_status.
function Api:request(method, path, params, body)
    if not self:configured() then
        return false, "Set the bridge address and token first."
    end
    local sink = {}
    local data = body and JSON.encode(body) or nil
    local request = {
        url = self.base_url .. path .. query(params),
        method = method,
        sink = ltn12.sink.table(sink),
        headers = {
            ["Authorization"] = "Bearer " .. self.token,
            ["Accept"] = "application/json",
        },
    }
    if data then
        request.source = ltn12.source.string(data)
        request.headers["Content-Type"] = "application/json"
        request.headers["Content-Length"] = tostring(#data)
    end
    -- Replies wait briefly on the bridge (it waits for T3 to react, and for suggestions),
    -- so allow a little longer than a plain read.
    socketutil:set_timeout(10, 30)
    local code, _, status = socket.skip(1, http.request(request))
    socketutil:reset_timeout()
    if type(code) ~= "number" then
        logger.warn("t3: request failed", method, path, code, status)
        return false, "Can't reach the bridge (" .. tostring(code or status) .. ")."
    end
    local text = table.concat(sink)
    local ok, decoded = pcall(JSON.decode, text)
    if not ok or type(decoded) ~= "table" then
        return false, "The bridge sent something unexpected (HTTP " .. code .. ").", code
    end
    if code ~= 200 then
        local msg = type(decoded.error) == "string" and decoded.error or ("HTTP " .. code)
        return false, msg, code
    end
    return true, decoded, code
end

--- Like request, but retries once on a network failure. Safe for writes because the
--- body carries a client_msg_id the bridge deduplicates on.
function Api:requestWithRetry(method, path, params, body)
    local ok, result, code = self:request(method, path, params, body)
    if not ok and code == nil and self:configured() then
        socket.sleep(1)
        ok, result, code = self:request(method, path, params, body)
    end
    return ok, result, code
end

function Api:threads(limit)
    return self:request("GET", "/v1/threads", { limit = limit or 8 })
end

function Api:projects()
    return self:request("GET", "/v1/projects")
end

function Api:thread(id, params)
    return self:request("GET", "/v1/threads/" .. id, params)
end

--- Quick check (no waiting) whether the thread changed since version v.
function Api:changedSince(id, v, progress, expand)
    return self:request("GET", "/v1/threads/" .. id .. "/wait",
        { v = v, timeout = 0, progress = progress, expand = expand })
end

function Api:listChangedSince(v, limit)
    return self:request("GET", "/v1/threads/wait", { v = v, timeout = 0, limit = limit or 8 })
end

function Api:reply(id, reply_id, text, progress, expand)
    return self:requestWithRetry("POST", "/v1/threads/" .. id .. "/reply", nil, {
        reply = reply_id, text = text, client_msg_id = Api.newMessageId(), progress = progress or nil,
        expand = expand,
    })
end

function Api:startThread(project_id, text)
    return self:requestWithRetry("POST", "/v1/threads", nil, {
        project = project_id, text = text, client_msg_id = Api.newMessageId(),
    })
end

return Api
