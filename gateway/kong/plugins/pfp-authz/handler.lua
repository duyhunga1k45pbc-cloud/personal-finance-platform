local http = require "resty.http"
local json = require "cjson.safe"

local PFPAuthz = {
  VERSION = "0.2.0",
  PRIORITY = 1000,
}

local function reject(status, message)
  return kong.response.exit(status, {
    message = message,
  })
end

function PFPAuthz:access(conf)
  local bearer = kong.request.get_header("authorization")

  if type(bearer) ~= "string"
      or not bearer:match("^[Bb]earer%s+%S+$") then
    return reject(401, "Missing bearer token")
  end

  local payload = json.encode({
    method = kong.request.get_method(),
    path = kong.request.get_path(),
  })

  if not payload then
    return reject(503, "Authorization unavailable")
  end

  local client = http.new()
  client:set_timeout(conf.timeout_ms)

  local response = client:request_uri(conf.authz_url, {
    method = "POST",
    body = payload,
    headers = {
      ["Content-Type"] = "application/json",
      ["Authorization"] = bearer,
      ["X-Gateway-Secret"] = conf.shared_secret,
    },
    ssl_verify = true,
    keepalive = false,
  })

  if not response or response.status ~= 200 then
    return reject(503, "Authorization unavailable")
  end

  local decision = json.decode(response.body or "")

  if type(decision) ~= "table"
      or type(decision.allow) ~= "boolean" then
    return reject(503, "Invalid authorization response")
  end

  if not decision.allow then
    return reject(403, "Access denied")
  end
end

return PFPAuthz
