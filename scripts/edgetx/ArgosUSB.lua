-- EdgeTX 2.12 USB bench display. Install in SCRIPTS/TOOLS/ArgosUSB.lua.
-- Only LCD, time and serial APIs: no model, channel, mixer or telemetry writes.
local buffer = ""
local dropping = false
local lastGreeting = nil
local lastReceived = nil
local received = 0
local lastSequence = 0
local stopped = false

local function elapsed(now, before)
  return (now - before) % 4294967296
end

local function accept(line, now)
  local digits = string.match(line, "^ARGOS_USB_PING (%d+)$")
  local sequence = tonumber(digits)
  if sequence and sequence >= 1 and sequence <= 120
      and digits == tostring(sequence) then
    received = math.min(received + 1, 9999)
    lastSequence = sequence
    lastReceived = now
    serialWrite("ARGOS_USB_ACK " .. digits .. "\n")
  end
end

local function run(event)
  if stopped or event == EVT_VIRTUAL_EXIT then
    stopped = true
    return 2
  end
  local now = getTime()
  lcd.clear()
  lcd.drawText(0, 0, "ARGOS USB", 0)
  if type(serialRead) ~= "function" or type(serialWrite) ~= "function" then
    lcd.drawText(0, 16, "Serial API missing", 0)
    return 0
  end

  if lastGreeting == nil or elapsed(now, lastGreeting) >= 50 then
    serialWrite("ARGOS_USB_DISPLAY_V1\n")
    lastGreeting = now
  end
  -- Bound work and memory even if a wrong serial peer sends continuous noise.
  local data = serialRead(64)
  for i = 1, #data do
    local c = string.sub(data, i, i)
    if c == "\n" then
      if not dropping then accept(buffer, now) end
      buffer = ""
      dropping = false
    elseif not dropping then
      if #buffer >= 40 then
        buffer = ""
        dropping = true
      else
        buffer = buffer .. c
      end
    end
  end

  local state = "WAITING FOR PC"
  if lastReceived ~= nil then
    if elapsed(now, lastReceived) < 150 then
      state = "RECEIVING"
    else
      state = "NO RECENT MESSAGE"
    end
  end
  lcd.drawText(0, 12, state, 0)
  lcd.drawText(0, 24, "Received: " .. received, 0)
  lcd.drawText(0, 36, "Last: " .. lastSequence, 0)
  lcd.drawText(0, 54, "DISPLAY ONLY / EXIT", 0)
  return 0
end

return { run = run }
