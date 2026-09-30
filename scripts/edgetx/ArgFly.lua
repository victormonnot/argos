-- Continuous yaw-assist protocol foundation; NOT hardware/flight validated.
-- Final profile contract: ARGOS FLY, internal CRSF CH1..16, external OFF,
-- crash flip CH7 fixed low. Only Val may replace yaw through the native gate.
-- Native L10 MUST latch deliberate takeover (>25% / 200 ms or >50% immediately)
-- until SC middle; the gate must also bypass Lua immediately above 50% stick.
-- No model writes, ARM, throttle, roll, pitch or motor commands are available.
-- A radio-issued ticket expires on its ISSUE clock: delayed USB cannot extend
-- its lease by arriving late. Expiry still needs Lua/native mixer execution;
-- this protocol does not establish a hardware failsafe or image-age bound.
local MAX_SEQUENCE = 2147483647
local session = nil
local generation = 0
local ticket = 0
local tickets = {}
local sequence = 0
local state = "M" -- Manual/rearm, enabled Target unavailable, Active, Fault.
local value, fresh, heartbeat = 0, 0, 0
local leaseIssued = nil
local lastAccepted = nil
local enabledAt = nil
local lastStatus = nil
local lastHello = nil
local lastClock = nil
local sawMiddle = false
local stickSince = nil
local buffer = ""
local dropping = false
local pending = nil

local function elapsed(now, before)
  -- EdgeTX uses Lua 5.3 with 32-bit integers/single-precision floats. Keep time
  -- math integer-only across getTime()'s signed rollover; all leases are <=1s.
  return (now - before) & MAX_SEQUENCE
end

local function withdraw()
  value, fresh, heartbeat = 0, 0, 0
  leaseIssued = nil
end

local function invalidate()
  generation = generation == MAX_SEQUENCE and 0 or generation + 1
  tickets = {}
  pending = nil
  lastStatus = nil
end

local function manual(nextState)
  if state ~= nextState or enabledAt ~= nil then invalidate() end
  state = nextState
  enabledAt = nil
  lastAccepted = nil
  stickSince = nil
  sawMiddle = false
  withdraw()
end

local function fault()
  manual("F")
end

local function write(line)
  local ok = pcall(serialWrite, line)
  if not ok then fault() end
  return ok
end

local function permitted()
  local ok, allowed, sc, rud, takeover = pcall(function()
    local info = model.getInfo()
    local internal, external = model.getModule(0), model.getModule(1)
    local flip = getOutputValue(6)
    local selector, stick = getValue("sc"), getValue("rud")
    local native = getLogicalSwitchValue(9) -- zero-based L10, mandatory.
    return type(info) == "table" and info.name == "ARGOS FLY"
      and type(internal) == "table" and internal.Type == 5
      and internal.firstChannel == 0 and internal.channelsCount == 16
      and type(external) == "table" and external.Type == 0
      and type(flip) == "number" and flip >= -1100 and flip <= -900
      and (selector == -1024 or selector == 0 or selector == 1024)
      and type(stick) == "number" and stick >= -1024 and stick <= 1024
      and type(native) == "boolean", selector, stick, native
  end)
  return ok and allowed == true, sc, rud, takeover
end

local function authority(now, sc, rud, native)
  if sc == 0 then
    if state ~= "M" then manual("M") end
    -- A middle observation must occur AFTER BEGIN/fault, never just startup-up.
    sawMiddle = session ~= nil
    stickSince = nil
  elseif sc ~= -1024 then
    manual("M")
  elseif state == "M" and sawMiddle and math.abs(rud) <= 102 and not native then
    invalidate()
    state = "T"
    sawMiddle = false
    enabledAt = now
    lastAccepted = nil
  end
  if state == "T" or state == "A" then
    local magnitude = math.abs(rud)
    if native or magnitude > 512 then
      fault()
    elseif magnitude > 256 then
      if stickSince == nil then stickSince = now end
      if elapsed(now, stickSince) >= 20 then fault() end
    else
      stickSince = nil
    end
    if enabledAt ~= nil and elapsed(now, lastAccepted or enabledAt) >= 100 then
      fault()
    elseif leaseIssued ~= nil and elapsed(now, leaseIssued) >= 30 then
      withdraw()
      state = "T"
      lastStatus = nil
    end
  end
end

local function number(text, maximum)
  local n = tonumber(text)
  if n and math.type(n) == "integer" and n >= 0 and n <= maximum
      and text == tostring(n) then
    return n
  end
  return nil
end

local function accept(line, now)
  local begin = string.match(line, "^AB1 ([0-9a-f]+)$")
  if begin and #begin == 8 and begin ~= "00000000" then
    if begin == session then return end
    session = begin
    sequence = 0
    manual("M")
    invalidate()
    sawMiddle = false
    return
  end

  local token, genText, ticketText, seqText, validText, valueText = string.match(
    line, "^AS1 ([0-9a-f]+) (%d+) (%d+) (%d+) ([01]) ([%-]?%d+)$")
  local gen, issuedTicket = number(genText, MAX_SEQUENCE), number(ticketText, MAX_SEQUENCE)
  local nextSequence, nextValue = number(seqText, MAX_SEQUENCE), tonumber(valueText)
  if (state ~= "A" and state ~= "T") or token ~= session or gen ~= generation
      or not nextSequence or nextSequence <= sequence
      or (pending and nextSequence <= pending.sequence)
      or not nextValue or math.type(nextValue) ~= "integer"
      or nextValue < -128 or nextValue > 128 or valueText ~= tostring(nextValue)
      or (validText == "0" and nextValue ~= 0) then return end
  for _, issued in ipairs(tickets) do
    if issued.number == issuedTicket and issued.generation == generation
        and elapsed(now, issued.clock) < 30 then
      -- Only publish the newest validated command once the complete bounded
      -- batch has been consumed. A queue cannot create multiple heartbeats.
      pending = {sequence = nextSequence, value = nextValue,
        valid = validText == "1", clock = issued.clock}
      return
    end
  end
end

local function readBatch(now)
  pending = nil
  for chunk = 1, 4 do
    local ok, data = pcall(serialRead, 64)
    if not ok or type(data) ~= "string" or #data > 64 then
      buffer = ""
      dropping = true
      fault()
      return
    end
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
          fault()
        else
          buffer = buffer .. c
        end
      end
    end
    if #data < 64 then return end
    -- A saturated read budget means the callback cannot establish the newest
    -- input. Withdraw and require a physical rearm instead of playing a queue.
    if chunk == 4 then
      buffer = ""
      dropping = true
      fault()
    end
  end
end

local function publish(now)
  if not pending or (state ~= "T" and state ~= "A") then return end
  sequence = pending.sequence
  lastAccepted = now
  if pending.valid then
    value, fresh = pending.value, 1024
    leaseIssued = pending.clock
    heartbeat = heartbeat <= 0 and 1024 or -1024
    if state ~= "A" then lastStatus = nil end
    state = "A"
  else
    withdraw()
    if state ~= "T" then lastStatus = nil end
    state = "T"
  end
  pending = nil
end

local function status(now)
  if lastHello == nil or elapsed(now, lastHello) >= 100 then
    if not write("ARGOS_YAW_STREAM_V1\n") then return end
    lastHello = now
  end
  if lastStatus == nil or elapsed(now, lastStatus) >= 10 then
    ticket = ticket == MAX_SEQUENCE and 0 or ticket + 1
    tickets[#tickets + 1] = {number = ticket, clock = now, generation = generation}
    if #tickets > 4 then table.remove(tickets, 1) end
    if write(string.format("AY1 %s %d %d %d %s\n", session or "00000000",
        generation, ticket, sequence, state)) then lastStatus = now end
  end
end

local function run()
  local allowed, sc, rud, native = permitted()
  if not allowed or type(serialRead) ~= "function" or type(serialWrite) ~= "function" then
    fault()
    return 0, 0, 0, 0
  end
  local now = getTime()
  if type(now) ~= "number" or math.type(now) ~= "integer"
      or now < -2147483647 - 1 or now > MAX_SEQUENCE then
    fault()
    return 0, 0, 0, 0
  end
  if lastClock ~= nil and elapsed(now, lastClock) >= 1073741824 then fault() end
  lastClock = now
  -- Expire and inspect native pilot authority BEFORE reading delayed USB bytes.
  authority(now, sc, rud, native)
  readBatch(now)
  publish(now)
  status(now)
  -- Seq is a bounded native-gate marker; the full ACK sequence is on the wire.
  return value, fresh, sequence > 0 and 1024 or 0, heartbeat
end

return {output = {"Val", "Fsh", "Seq", "Hbt"}, run = run}
