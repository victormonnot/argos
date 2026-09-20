-- Bench mixer only: copied model "ARGOS USB", both RF modules OFF, no aircraft.
-- Install in SCRIPTS/MIXES/ArgMix.lua and use Val only on an unused CH32.
-- Timeout requires run() to keep executing; this is NOT a flight failsafe.
local session = nil
local sequence = 0
local value = 0
local fresh = 0
local lastReceived = nil
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
    return type(info) == "table" and info.name == "ARGOS USB"
      and type(internal) == "table" and internal.Type == 0
      and type(external) == "table" and external.Type == 0
  end)
  return ok and allowed == true
end

local function reset()
  session = nil
  sequence = 0
  value = 0
  fresh = 0
  lastReceived = nil
  lastHello = nil
  buffer = ""
  dropping = false
end

local function accept(line, now)
  local begin = string.match(line, "^ARGOS_MIX_BEGIN ([0-9a-f]+)$")
  if begin and #begin == 8 then
    session = begin
    sequence = 0
    value = 0
    fresh = 0
    lastReceived = nil
    serialWrite("ARGOS_MIX_READY " .. session .. "\n")
    return
  end

  local token, digits, textValue = string.match(line,
    "^ARGOS_MIX_SET ([0-9a-f]+) (%d+) ([%-]?%d+)$")
  local nextSequence = tonumber(digits)
  local nextValue = tonumber(textValue)
  if token == session and session ~= nil and nextSequence
      and nextSequence > sequence and nextSequence <= 120
      and digits == tostring(nextSequence)
      and (textValue == "-256" or textValue == "0" or textValue == "256") then
    sequence = nextSequence
    value = nextValue
    fresh = 1024
    lastReceived = now
    serialWrite("ARGOS_MIX_ACK " .. session .. " " .. digits .. " " .. textValue .. "\n")
  end
end

local function run()
  if not permitted() or type(serialRead) ~= "function" or type(serialWrite) ~= "function" then
    reset()
    return 0, 0, 0
  end
  local now = getTime()
  if lastHello == nil or elapsed(now, lastHello) >= 50 then
    serialWrite("ARGOS_USB_MIX_BENCH_V1\n")
    lastHello = now
  end
  -- Fixed read/work budget; discard an oversized line through its delimiter.
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
    lastReceived = nil
    serialWrite("ARGOS_MIX_IDLE " .. session .. " " .. sequence .. "\n")
  end
  return value, fresh, sequence
end

return { output = { "Val", "Fsh", "Seq" }, run = run }
