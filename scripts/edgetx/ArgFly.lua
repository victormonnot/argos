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
local MAX_VALUE = 205 -- round(20% * 1024), continuous V3 only.
local session = nil
local generation = 0
local ticket = 0
local tickets = {}
local sequence = 0
local state = "M" -- Manual/rearm, enabled Target unavailable, Active, Fault.
local cause = "S" -- V2 status: startup, manual, waiting, active, target, expiry...
local value, fresh, heartbeat = 0, 0, 0
local leaseIssued = nil
local lastAccepted = nil
local enabledAt = nil
local lastStatus = nil
local lastPilot = nil
local lastHello = nil
local lastClock = nil
local sawMiddle = false
local stickSince = nil
local buffer = ""
local dropping = false
local pending = nil
local blockedActive = false
local lastBlockedAt = nil
local blockedCallbacks = 0

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

local function manual(nextState, nextCause)
  if state ~= nextState or enabledAt ~= nil then invalidate() end
  if cause ~= nextCause then lastStatus = nil end
  state = nextState
  cause = nextCause
  enabledAt = nil
  lastAccepted = nil
  stickSince = nil
  sawMiddle = false
  withdraw()
end

local function fault(reason)
  manual("F", reason)
end

local function write(line)
  local ok = pcall(serialWrite, line)
  if not ok then fault("I") end
  return ok
end

local function validClock(now)
  return type(now) == "number" and type(math.type) == "function"
    and math.type(now) == "integer" and now >= -2147483647 - 1 and now <= MAX_SEQUENCE
end

local function observed(value)
  -- Diagnostics stay inside the existing 64-byte ASCII framing limit.
  return string.sub(string.gsub(tostring(value), "[^A-Za-z0-9_.:+%-]", "_"), 1, 24)
end

local function blocked(reason, detail, now)
  if type(serialWrite) ~= "function" then return end
  local emit = not blockedActive
  if blockedActive then
    if validClock(now) and lastBlockedAt ~= nil then
      emit = elapsed(now, lastBlockedAt) >= 100
    else
      -- A broken clock cannot give a real-time rate; bound diagnostic attempts.
      blockedCallbacks = blockedCallbacks + 1
      emit = blockedCallbacks >= 20
    end
  end
  if emit then
    pcall(serialWrite, "ARGOS_YAW_BLOCKED " .. reason .. " " .. observed(detail) .. "\n")
    blockedActive = true
    lastBlockedAt = validClock(now) and now or nil
    blockedCallbacks = 0
  end
end

local function permitted()
  local ok, allowed, sc, rud, takeover, reason, detail = pcall(function()
    local info = model.getInfo()
    if type(info) ~= "table" or info.name ~= "ARGOS FLY" then
      return false, nil, nil, nil, "model", type(info) == "table" and info.name or type(info)
    end
    local internal = model.getModule(0)
    if type(internal) ~= "table" or internal.Type ~= 5
        or internal.firstChannel ~= 0 or internal.channelsCount ~= 16 then
      local value = type(internal) == "table" and (tostring(internal.Type) .. ":"
        .. tostring(internal.firstChannel) .. ":" .. tostring(internal.channelsCount)) or type(internal)
      return false, nil, nil, nil, "internal_rf", value
    end
    local external = model.getModule(1)
    if type(external) ~= "table" or external.Type ~= 0 then
      return false, nil, nil, nil, "external_rf", type(external) == "table" and external.Type or type(external)
    end
    local flip = getOutputValue(6)
    if type(flip) ~= "number" or not (flip >= -1100 and flip <= -900) then
      return false, nil, nil, nil, "crash_flip", flip
    end
    local selector, stick = getValue("sc"), getValue("rud")
    if selector ~= -1024 and selector ~= 0 and selector ~= 1024 then
      return false, nil, nil, nil, "selector", selector
    end
    if type(stick) ~= "number" or not (stick >= -1024 and stick <= 1024) then
      return false, nil, nil, nil, "stick", stick
    end
    local native = getLogicalSwitchValue(9) -- zero-based L10, mandatory.
    if type(native) ~= "boolean" then
      return false, nil, nil, nil, "logical_switch", native
    end
    return true, selector, stick, native
  end)
  if not ok then
    local message = tostring(allowed)
    message = string.match(message, "global '([^']+)'")
      or string.gsub(message, "^[^:]+:%d+:%s*", "")
    return false, nil, nil, nil, "api_exception", message
  end
  return allowed == true, sc, rud, takeover, reason, detail
end

local function authority(now, sc, rud, native)
  if sc == 0 then
    if state ~= "M" then manual("M", "M") end
    -- A middle observation must occur AFTER BEGIN/fault, never just startup-up.
    sawMiddle = session ~= nil
    stickSince = nil
  elseif sc ~= -1024 then
    manual("M", "M")
  elseif state == "M" and sawMiddle and math.abs(rud) <= 102 and not native then
    invalidate()
    state = "T"
    cause = "W"
    sawMiddle = false
    enabledAt = now
    lastAccepted = nil
  end
  if state == "T" or state == "A" then
    local magnitude = math.abs(rud)
    if native or magnitude > 512 then
      fault("P")
    elseif magnitude > 256 then
      if stickSince == nil then stickSince = now end
      if elapsed(now, stickSince) >= 20 then fault("P") end
    else
      stickSince = nil
    end
    if enabledAt ~= nil and elapsed(now, lastAccepted or enabledAt) >= 100 then
      fault("L")
    elseif leaseIssued ~= nil and elapsed(now, leaseIssued) >= 30 then
      withdraw()
      state = "T"
      cause = "E"
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
    manual("M", "S")
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
      or nextValue < -MAX_VALUE or nextValue > MAX_VALUE or valueText ~= tostring(nextValue)
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
      fault("I")
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
          fault("O")
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
      fault("O")
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
    cause = "A"
  else
    withdraw()
    if state ~= "T" or cause ~= "T" then lastStatus = nil end
    state = "T"
    cause = "T"
  end
  pending = nil
end

local function pilotSample(now, rud)
  if lastPilot ~= nil and elapsed(now, lastPilot) < 10 then return end
  lastPilot = now
  -- Optional, <=10 Hz pre-mix observation. Failures never alter command state.
  -- AP1 alone has a 96-byte output bound; commands/read budgets remain 64.
  pcall(function()
    local ail, ele, thr = getValue("ail"), getValue("ele"), getValue("thr")
    for _, source in ipairs({ail, ele, thr, rud}) do
      if type(source) ~= "number" or math.type(source) ~= "integer"
          or source < -1024 or source > 1024 then return end
    end
    -- Check missing values explicitly: ipairs ends at the first nil.
    if ail == nil or ele == nil or thr == nil or rud == nil then return end
    local enabled = state == "T" or state == "A"
    serialWrite(string.format("AP1 %s %d %d %d %s%s%s%sN%d0 %d %d %d %d %d 0\n",
      session or "00000000", generation, ticket, sequence, state, cause,
      enabled and "Y" or "N", enabled and "A" or "N", fresh > 0 and 1 or 0,
      ail, ele, thr, rud, value))
  end)
end

local function status(now, rud)
  if lastHello == nil or elapsed(now, lastHello) >= 100 then
    if not write("ARGOS_YAW_STREAM_V3\n") then return end
    lastHello = now
  end
  if lastStatus == nil or elapsed(now, lastStatus) >= 10 then
    ticket = ticket == MAX_SEQUENCE and 0 or ticket + 1
    tickets[#tickets + 1] = {number = ticket, clock = now, generation = generation}
    -- EdgeTX Pocket omits the table library. Keep the four newest tickets
    -- with bounded assignments, preserving each ticket's original issue clock.
    if #tickets > 4 then
      for i = 1, 4 do tickets[i] = tickets[i + 1] end
      tickets[5] = nil
    end
    if write(string.format("AY1 %s %d %d %d %s %s\n", session or "00000000",
        generation, ticket, sequence, state, cause)) then
      lastStatus = now
      pilotSample(now, rud)
    end
  end
end

local function run()
  local allowed, sc, rud, native, reason, detail = permitted()
  local clockOK, now = pcall(getTime)
  if not allowed then
    fault("G")
    blocked(reason, detail, clockOK and now or nil)
    return 0, 0, 0, 0
  end
  if type(serialRead) ~= "function" or type(serialWrite) ~= "function" then
    fault("I")
    blocked("serial_api", type(serialRead) .. ":" .. type(serialWrite), clockOK and now or nil)
    return 0, 0, 0, 0
  end
  if not clockOK or not validClock(now) then
    fault("C")
    blocked("clock", clockOK and now or "api_exception", nil)
    return 0, 0, 0, 0
  end
  if lastClock ~= nil and elapsed(now, lastClock) >= 1073741824 then
    fault("C")
    lastClock = now
    blocked("clock", "regressed", now)
    return 0, 0, 0, 0
  end
  blockedActive, lastBlockedAt, blockedCallbacks = false, nil, 0
  lastClock = now
  -- Expire and inspect native pilot authority BEFORE reading delayed USB bytes.
  authority(now, sc, rud, native)
  readBatch(now)
  publish(now)
  status(now, rud)
  -- Seq is a bounded native-gate marker; the full ACK sequence is on the wire.
  return value, fresh, sequence > 0 and 1024 or 0, heartbeat
end

return {output = {"Val", "Fsh", "Seq", "Hbt"}, run = run}
