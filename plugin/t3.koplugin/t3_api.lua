--[[--
HTTP client for the T3 KOde bridge (docs/bridge-api.md).

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
    return self.base_url ~= "" -- a token is optional (e.g. a bridge on a trusted network)
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

-- HTTPS. KOReader's LuaSec defaults to verify = "none", and neither KOReader nor a Kobo
-- ships CA certificates, so a plain https:// request would encrypt but trust anyone.
-- Check the chain against Mozilla's roots (cacert.pem, from https://curl.se/docs/caextract.html)
-- and the name against the certificate, which LuaSec leaves to the caller.
Api.cafile = (debug.getinfo(1, "S").source:match("^@(.*)/[^/]*$") or ".") .. "/cacert.pem"

-- RFC 6125: a wildcard stands for exactly one leftmost label.
local function nameMatches(pattern, host)
    pattern, host = pattern:lower(), host:lower()
    if pattern == host then return true end
    local suffix = pattern:match("^%*(%..+%..+)$")
    return suffix ~= nil and host:sub(-#suffix) == suffix
        and not host:sub(1, -#suffix - 1):find("[%.]") and #host > #suffix
end

local function certCoversHost(cert, host)
    local ok, exts = pcall(cert.extensions, cert)
    local san = ok and exts and exts["2.5.29.17"]
    if not san then return false end -- names live in subjectAltName; CN fallback is obsolete
    for _, name in ipairs(san.dNSName or {}) do
        if nameMatches(name, host) then return true end
    end
    for _, ip in ipairs(san.iPAddress or {}) do
        if ip == host then return true end
    end
    return false
end

local function verifiedHttpsCreate()
    local https = require("ssl.https")
    local make = https.tcp({ verify = { "peer", "fail_if_no_peer_cert" }, cafile = Api.cafile })
    return function()
        local conn = make()
        local connect = conn.connect
        function conn:connect(host, port)
            connect(self, host, port) -- raises if the chain doesn't verify
            local cert = self.sock:getpeercertificate()
            if not (cert and certCoversHost(cert, host)) then
                self.sock:close()
                return nil, "certificate is not for " .. host
            end
            return 1
        end
        return conn
    end
end

local function newRequest(url, method, sink)
    local request = { url = url, method = method, sink = sink, headers = {} }
    if url:match("^[Hh][Tt][Tt][Pp][Ss]://") then
        request.create = verifiedHttpsCreate()
    end
    return request
end

-- Turn LuaSec's terse failures into something a reader can act on.
local function networkError(err)
    err = tostring(err)
    if err:find("certificate verify failed", 1, true) then
        return "The bridge's HTTPS certificate isn't trusted (" .. err .. "). Is the device's clock right?"
    elseif err:find("certificate is not for", 1, true) then
        return "The bridge's HTTPS " .. err .. "."
    end
    return "Can't reach the bridge (" .. err .. ")."
end

--- Returns ok, body_or_error_message, http_status.
--- `timeouts`: { connect, total } seconds, instead of the usual 10 and 30.
function Api:request(method, path, params, body, timeouts)
    if not self:configured() then
        return false, "Set the bridge address first (Tools → T3 KOde → menu → Settings)."
    end
    local sink = {}
    local data = body and JSON.encode(body) or nil
    local request = newRequest(self.base_url .. path .. query(params), method, ltn12.sink.table(sink))
    request.headers["Accept"] = "application/json"
    if self.token ~= "" then
        request.headers["Authorization"] = "Bearer " .. self.token
    end
    if data then
        request.source = ltn12.source.string(data)
        request.headers["Content-Type"] = "application/json"
        request.headers["Content-Length"] = tostring(#data)
    end
    -- Replies wait briefly on the bridge (it waits for T3 to react, and for suggestions),
    -- so allow a little longer than a plain read.
    socketutil:set_timeout(timeouts and timeouts[1] or 10, timeouts and timeouts[2] or 30)
    local code, _, status = socket.skip(1, http.request(request))
    socketutil:reset_timeout()
    if type(code) ~= "number" then
        logger.warn("t3: request failed", method, path, code, status)
        return false, networkError(code or status)
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

--- Download to `filepath` (written to a temporary name first).
--- Returns ok, error_or_content_type.
function Api:download(path, params, filepath)
    if not self:configured() then return false, "not configured" end
    local tmp = filepath .. ".part"
    local fh = io.open(tmp, "wb")
    if not fh then return false, "can't write " .. tmp end
    local request = newRequest(self.base_url .. path .. query(params), "GET",
        ltn12.sink.file(fh)) -- closes the file when done
    if self.token ~= "" then request.headers["Authorization"] = "Bearer " .. self.token end
    socketutil:set_timeout(15, 60)
    local code, headers = socket.skip(1, http.request(request))
    socketutil:reset_timeout()
    if code ~= 200 then
        -- The bridge explains failures in a JSON body, which landed in the file.
        local msg = "HTTP " .. tostring(code)
        local efh = io.open(tmp, "rb")
        if efh then
            local ok, decoded = pcall(JSON.decode, efh:read(4096) or "")
            efh:close()
            if ok and type(decoded) == "table" and type(decoded.error) == "string" then msg = decoded.error end
        end
        if type(code) ~= "number" then msg = networkError(code) end
        os.remove(tmp)
        return false, msg
    end
    os.rename(tmp, filepath)
    return true, type(headers) == "table" and headers["content-type"] or nil
end

--- Quick check that the bridge answers (short timeouts, for trying again and again).
function Api:health()
    return (self:request("GET", "/v1/health", nil, nil, { 4, 6 }))
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
--- `after`: the view starts after this cursor (see Api:thread), not at the default window.
function Api:changedSince(id, v, progress, expand, after)
    return self:request("GET", "/v1/threads/" .. id .. "/wait",
        { v = v, timeout = 0, progress = progress, expand = expand, after = after })
end

function Api:listChangedSince(v, limit)
    return self:request("GET", "/v1/threads/wait", { v = v, timeout = 0, limit = limit or 8 })
end

--- `mode`: "plan" or "default" switches plan mode before sending typed text; nil leaves it.
--- `deliver`: while a turn runs, "queue" typed text for after it or "steer" the turn with it.
function Api:reply(id, reply_id, text, progress, expand, mode, deliver)
    return self:requestWithRetry("POST", "/v1/threads/" .. id .. "/reply", nil, {
        reply = reply_id, text = text, client_msg_id = Api.newMessageId(), progress = progress or nil,
        expand = expand, mode = mode, deliver = deliver,
    })
end

--- A queued message: "send" it now, "steer" the running turn with it, "remove" it, or
--- "resume" a paused queue (no `item`). `item`: its id from the view's queue.
function Api:queueAction(id, action, item)
    return self:request("POST", "/v1/threads/" .. id .. "/queue", nil, { action = action, item = item })
end

--- Answer a secret request. Not retried: a retry after a lost reply would be refused as
--- already answered. The value is only in this request's body.
function Api:answerSecret(id, secret)
    return self:request("POST", "/v1/threads/" .. id .. "/secret", nil, { secret = secret })
end

function Api:settle(id, settled)
    return self:requestWithRetry("POST", "/v1/threads/" .. id .. "/settle", nil, { settled = settled })
end

--- Pin, unpin, snooze, unsnooze, mark_unread, archive, rename or auto_title a thread.
--- `snooze`: a preset (hour, three-hours, evening, tomorrow, next-week), for "snooze".
--- `title`: the new title, for "rename".
function Api:threadAction(id, action, snooze, title)
    return self:requestWithRetry("POST", "/v1/threads/" .. id .. "/action", nil,
        { action = action, snooze = snooze, title = title })
end

--- `model`: an id from Api:models(), or nil for the project's default.
--- `mode`: "plan" starts it in plan mode.
function Api:startThread(project_id, text, model, mode)
    return self:requestWithRetry("POST", "/v1/threads", nil, {
        project = project_id, text = text, model = model, mode = mode, client_msg_id = Api.newMessageId(),
    })
end

--- Models to choose from, for a thread (`{thread = id}`) or a new one (`{project = id}`).
function Api:models(params)
    return self:request("GET", "/v1/models", params)
end

function Api:setModel(id, model, progress)
    return self:requestWithRetry("POST", "/v1/threads/" .. id .. "/model", nil, {
        model = model, client_msg_id = Api.newMessageId(), progress = progress or nil,
    })
end

--- The thread's preview browser tabs and open devices:
--- { tabs = { {id, title, url, status, viewable}, … }, devices = { {id, name, kind, …}, … } }.
function Api:browserTabs(id)
    return self:request("GET", "/v1/threads/" .. id .. "/browser")
end

--- Save what a browser tab shows now to `filepath`. Returns ok, error_or_content_type.
function Api:browserSnapshot(id, tab_id, width, filepath)
    return self:download("/v1/threads/" .. id .. "/browser/snapshot", { tab = tab_id, w = width }, filepath)
end

--- Save what a simulator or emulator shows now to `filepath`. Returns ok, error_or_content_type.
function Api:deviceSnapshot(id, device_id, width, filepath)
    return self:download("/v1/threads/" .. id .. "/device/snapshot", { device = device_id, w = width }, filepath)
end

--- Open pull requests, grouped: `scope` "mine" (yours and review requests) or "all".
--- T3 asks the host for them, which can take a while for everyone's, so the bridge
--- may say nothing for most of a minute.
function Api:pullRequests(scope)
    return self:request("GET", "/v1/prs", { scope = scope }, nil, { 60, 90 })
end

--- One pull request's page, `pr` being {project, repo, number, host} from a list or thread:
--- { title, number, repo, url, status, html }.
function Api:pullRequest(pr)
    return self:request("GET", "/v1/prs/detail",
        { project = pr.project, repo = pr.repo, number = pr.number, host = pr.host }, nil, { 60, 90 })
end

return Api
