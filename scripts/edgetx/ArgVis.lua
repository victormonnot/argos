-- VISION MIXER BENCH ONLY: copied model "ARGOS VIS", both RF modules OFF.
-- Install as SCRIPTS/MIXES/ArgVis.lua; Val belongs only on unused CH32 through
-- the separately configured native gate. This is not an aircraft controller.
-- Expiry needs run() to execute; neither this timer nor an ACK proves a flight
-- failsafe. TTL uses the radio receipt clock, not the source image timestamp.
local session = nil
local active = false
local started = nil
local sequence = 0
local value = 0
local fresh = 0
local heartbeat = 0
local lastReceived = nil
local lifetime = nil
local lastHello = nil
local buffer = ""
local dropping = false

local function elapsed(now, before)
  return (now - before) % 4294967296
end

local function permitted()
  local ok, allowed = pcall(function()
    local info = model.getInfo()
    local internal = model.getModule(0)
    local external = model.getModule(1)
    return type(info) == "table" and info.name == "ARGOS VIS"
      and type(internal) == "table" and internal.Type == 0
      and type(external) == "table" and external.Type == 0
  end)
  return ok and allowed == true
end

local function reset()
  session = nil
  active = false
  started = nil
  sequence = 0
  value = 0
  fresh = 0
  heartbeat = 0
  lastReceived = nil
  lifetime = nil
  lastHello = nil
  buffer = ""
  dropping = false
end

local function write(line)
  local ok = pcall(serialWrite, line)
  if not ok then reset() end
  return ok
end

local function expire(now)
  if active and (elapsed(now, started) >= 3000
      or (lastReceived ~= nil and elapsed(now, lastReceived) >= lifetime)) then
    active = false
    value = 0
    fresh = 0
    heartbeat = 0
    lastReceived = nil
    lifetime = nil
    -- Keep the token and sequence: late SET or a repeated BEGIN must not revive
    -- the expired session. A new, distinct BEGIN is required to start again.
    write("ARGOS_VISION_IDLE " .. session .. " " .. sequence .. "\n")
  end
end

local function accept(line, now)
  local begin = string.match(line, "^ARGOS_VISION_BEGIN ([0-9a-f]+)$")
  if begin and #begin == 8 then
    if begin == session then return end
    session = begin
    active = true
    started = now
    sequence = 0
    value = 0
    fresh = 0
    heartbeat = 0
    lastReceived = nil
    lifetime = nil
    write("ARGOS_VISION_READY " .. session .. "\n")
    return
  end

  local token, digits, textValue, textTTL = string.match(line,
    "^ARGOS_VISION_SET ([0-9a-f]+) (%d+) ([%-]?%d+) (%d+)$")
  local nextSequence = tonumber(digits)
  local nextValue = tonumber(textValue)
  local nextTTL = tonumber(textTTL)
  if active and token == session and nextSequence and nextValue and nextTTL
      and nextSequence > sequence and nextSequence <= 300
      and digits == tostring(nextSequence)
      and nextValue >= -128 and nextValue <= 128
      and textValue ~= "-0" and textValue == tostring(nextValue)
      and nextTTL >= 1 and nextTTL <= 20 and textTTL == tostring(nextTTL) then
    sequence = nextSequence
    value = nextValue
    fresh = 1024
    heartbeat = heartbeat <= 0 and 1024 or -1024
    lastReceived = now
    lifetime = nextTTL
    write("ARGOS_VISION_ACK " .. session .. " " .. digits .. " "
      .. textValue .. " " .. textTTL .. "\n")
  end
end

local function run()
  if not permitted() or type(serialRead) ~= "function" or type(serialWrite) ~= "function" then
    reset()
    return 0, 0, 0, 0
  end
  local now = getTime()
  -- Evaluate expiry before serial input. A delayed SET queued at the deadline
  -- cannot refresh the old output or toggle the native gate's heartbeat.
  expire(now)
  if lastHello == nil or elapsed(now, lastHello) >= 50 then
    if not write("ARGOS_USB_VISION_BENCH_V1\n") then return 0, 0, 0, 0 end
    lastHello = now
  end
  local ok, data = pcall(serialRead, 64)
  if not ok or type(data) ~= "string" or #data > 64 then
    reset()
    return 0, 0, 0, 0
  end
  -- Bound both callback work and line storage. Discard an oversized line
  -- through its newline, including any valid-looking command suffix.
  for i = 1, #data do
    local c = string.sub(data, i, i)
    if c == "\n" then
      if not dropping then accept(buffer, now) end
      buffer = ""
      dropping = false
    elseif not dropping then
      if #buffer >= 64 then
        buffer = ""
        dropping = true
      else
        buffer = buffer .. c
      end
    end
  end
  return value, fresh, sequence, heartbeat
end

return { output = { "Val", "Fsh", "Seq", "Hbt" }, run = run }
