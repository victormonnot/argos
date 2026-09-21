-- DISARMED RF YAW BENCH ONLY: model "ARGOS RF", propellers removed, USB-only FC.
-- Internal CRSF CH1..16, external OFF. Use the separately configured native gate.
-- getOutputValue reads the most recent channelOutputs after limits/curves.
-- These samples do not prove the complete model or aircraft state: reviewed
-- configuration and fresh host MSP checks are required.
-- The 300 ms timeout needs run() to execute; it is not a flight failsafe.
local session = nil
local sequence = 0
local value = 0
local fresh = 0
local heartbeat = 0
local lastReceived = nil
local lastHello = nil
local buffer = ""
local dropping = false

local function elapsed(now, before)
  return (now - before) % 4294967296
end

local function low(index)
  local output = getOutputValue(index)
  -- Numeric comparisons also reject NaN and either infinity.
  return type(output) == "number" and output >= -1100 and output <= -900
end

local function permitted()
  local ok, allowed = pcall(function()
    local info = model.getInfo()
    local internal = model.getModule(0)
    local external = model.getModule(1)
    return type(info) == "table" and info.name == "ARGOS RF"
      and type(internal) == "table" and internal.Type == 5
      and internal.firstChannel == 0 and internal.channelsCount == 16
      and type(external) == "table" and external.Type == 0
      and low(2) and low(4) and low(6)
  end)
  return ok and allowed == true
end

local function reset()
  session = nil
  sequence = 0
  value = 0
  fresh = 0
  heartbeat = 0
  lastReceived = nil
  lastHello = nil
  buffer = ""
  dropping = false
end

local function accept(line, now)
  local begin = string.match(line, "^ARGOS_RF_BEGIN ([0-9a-f]+)$")
  if begin and #begin == 8 then
    session = begin
    sequence = 0
    value = 0
    fresh = 0
    heartbeat = 0
    lastReceived = nil
    serialWrite("ARGOS_RF_READY " .. session .. "\n")
    return
  end

  local token, digits, textValue = string.match(line,
    "^ARGOS_RF_SET ([0-9a-f]+) (%d+) ([%-]?%d+)$")
  local nextSequence = tonumber(digits)
  if token == session and session ~= nil and nextSequence
      and nextSequence > sequence and nextSequence <= 120
      and digits == tostring(nextSequence)
      and (textValue == "-128" or textValue == "0" or textValue == "128") then
    sequence = nextSequence
    value = tonumber(textValue)
    fresh = 1024
    heartbeat = heartbeat <= 0 and 1024 or -1024
    lastReceived = now
    serialWrite("ARGOS_RF_ACK " .. session .. " " .. digits .. " " .. textValue .. "\n")
  end
end

local function run()
  if not permitted() or type(serialRead) ~= "function" or type(serialWrite) ~= "function" then
    reset()
    return 0, 0, 0, 0
  end
  local now = getTime()
  if lastHello == nil or elapsed(now, lastHello) >= 50 then
    serialWrite("ARGOS_RF_YAW_BENCH_V1\n")
    lastHello = now
  end
  -- Bound memory/work; discard an oversized line through its next newline.
  local data = serialRead(64)
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
  if lastReceived ~= nil and elapsed(now, lastReceived) >= 30 then
    value = 0
    fresh = 0
    heartbeat = 0
    lastReceived = nil
    serialWrite("ARGOS_RF_IDLE " .. session .. " " .. sequence .. "\n")
  end
  return value, fresh, sequence, heartbeat
end

return { output = { "Val", "Fsh", "Seq", "Hbt" }, run = run }
