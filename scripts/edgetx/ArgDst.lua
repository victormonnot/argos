-- Experimental, isolated apparent-distance profile: ARGOS DST only.
-- SC up: yaw, SC down with SB up/ANGLE: yaw + bounded pitch; middle: manual.
-- Six outputs use EdgeTX MAX_SCRIPT_OUTPUTS=6. Native gates must independently
-- withdraw each axis independently during stick input; Lua cannot guarantee execution.
-- This does not control throttle, arming, roll or motors.
local MAX_SEQUENCE = 2147483647
local MAX_VALUE = 205
local MAX_PITCH = 51
local mode = "N"
local session = nil
local generation = 0
local ticket = 0
local tickets = {}
local sequence = 0
local state = "M" -- Manual/rearm, enabled Target unavailable, Active, Fault.
local cause = "S" -- V3 status: startup, manual, waiting, active, target, expiry...
local value, fresh, heartbeat = 0, 0, 0
local pitch, pitchFresh = 0, 0
-- Phase N: outside mode; A: automatic; M: stick; R: fresh ticket wait.
local yawAxis = {phase = "N"}
local pitchAxis = {phase = "N"}
local leaseIssued = nil
local lastAccepted = nil
local enabledAt = nil
local lastStatus = nil
local lastPilot = nil
local lastHello = nil
local lastClock = nil
local sawMiddle = false
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
  pitch, pitchFresh = 0, 0
  leaseIssued = nil
end

local function invalidate()
  generation = generation == MAX_SEQUENCE and 0 or generation + 1
  tickets = {}
  pending = nil
  lastStatus = nil
end

local function phaseTo(axis, nextPhase)
  if axis.phase ~= nextPhase then lastStatus = nil end
  axis.phase = nextPhase
end

local function resetAxis(axis, nextPhase)
  phaseTo(axis, nextPhase)
  axis.centeredAt, axis.resumeAt, axis.resumeTicket = nil, nil, nil
end

local function manual(nextState, nextCause)
  if state ~= nextState or enabledAt ~= nil then invalidate() end
  if cause ~= nextCause then lastStatus = nil end
  state = nextState
  mode = "N"
  resetAxis(yawAxis, "N")
  resetAxis(pitchAxis, "N")
  cause = nextCause
  enabledAt = nil
  lastAccepted = nil
  sawMiddle = false
  withdraw()
end

local function axisAuthority(axis, now, stick)
  local magnitude = math.abs(stick)
  if magnitude > 102 or (axis.phase == "R" and magnitude > 51) then
    -- Native channel gates detect this independently, including when Lua stalls.
    -- Any amplitude/duration temporarily releases this axis, without changing
    -- the radio generation, selected person or distance reference.
    resetAxis(axis, "M")
  elseif axis.phase == "M" then
    if magnitude <= 51 then
      if axis.centeredAt == nil then axis.centeredAt = now end
      if elapsed(now, axis.centeredAt) >= 20 then
        phaseTo(axis, "R")
        axis.resumeAt, axis.resumeTicket = now, ticket
        axis.centeredAt = nil
      end
    else
      axis.centeredAt = nil
    end
  end
end

local function allowAxis(axis)
  local resume = axis.phase == "R" and axis.resumeAt ~= nil and axis.resumeTicket ~= nil
    and elapsed(pending.clock, axis.resumeAt) < 1073741824
    and elapsed(pending.ticket, axis.resumeTicket) > 0
    and elapsed(pending.ticket, axis.resumeTicket) < 1073741824
  if resume then resetAxis(axis, "A") end
  return axis.phase == "A"
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
  -- Diagnostics stay inside the existing 96-byte ASCII framing limit.
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
    pcall(serialWrite, "ARGOS_DISTANCE_BLOCKED " .. reason .. " " .. observed(detail) .. "\n")
    blockedActive = true
    lastBlockedAt = validClock(now) and now or nil
    blockedCallbacks = 0
  end
end

local function permitted()
  local ok, allowed, sc, rud, ele, sb, reason, detail = pcall(function()
    local info = model.getInfo()
    if type(info) ~= "table" or info.name ~= "ARGOS DST" then
      return false, nil, nil, nil, nil, "model", type(info) == "table" and info.name or type(info)
    end
    local internal = model.getModule(0)
    if type(internal) ~= "table" or internal.Type ~= 5
        or internal.firstChannel ~= 0 or internal.channelsCount ~= 16 then
      local value = type(internal) == "table" and (tostring(internal.Type) .. ":"
        .. tostring(internal.firstChannel) .. ":" .. tostring(internal.channelsCount)) or type(internal)
      return false, nil, nil, nil, nil, "internal_rf", value
    end
    local external = model.getModule(1)
    if type(external) ~= "table" or external.Type ~= 0 then
      return false, nil, nil, nil, nil, "external_rf", type(external) == "table" and external.Type or type(external)
    end
    local flip = getOutputValue(6)
    if type(flip) ~= "number" or not (flip >= -1100 and flip <= -900) then
      return false, nil, nil, nil, nil, "crash_flip", flip
    end
    local selector, stick = getValue("sc"), getValue("rud")
    local elevator, angleSwitch = getValue("ele"), getValue("sb")
    if selector ~= -1024 and selector ~= 0 and selector ~= 1024 then
      return false, nil, nil, nil, nil, "selector", selector
    end
    if type(stick) ~= "number" or not (stick >= -1024 and stick <= 1024) then
      return false, nil, nil, nil, nil, "stick", stick
    end
    if type(elevator) ~= "number" or not (elevator >= -1024 and elevator <= 1024) then
      return false, nil, nil, nil, nil, "pitch", elevator
    end
    if angleSwitch ~= -1024 and angleSwitch ~= 0 and angleSwitch ~= 1024 then
      return false, nil, nil, nil, nil, "mode", angleSwitch
    end
    return true, selector, stick, elevator, angleSwitch
  end)
  if not ok then
    local message = tostring(allowed)
    message = string.match(message, "global '([^']+)'")
      or string.gsub(message, "^[^:]+:%d+:%s*", "")
    return false, nil, nil, nil, nil, "api_exception", message
  end
  return allowed == true, sc, rud, ele, sb, reason, detail
end

local function authority(now, sc, rud, ele, sb)
  local requested = sc == -1024 and "Y" or (sc == 1024 and "D" or "N")
  if sc == 0 then
    if state ~= "M" then manual("M", "M") end
    sawMiddle = session ~= nil
  elseif mode ~= "N" and requested ~= mode then
    -- Crossing directly between enabled modes never changes authority in place.
    manual("M", "M")
  elseif requested == "D" and sb ~= -1024 then
    fault("G")
  elseif state == "M" and sawMiddle and math.abs(rud) <= 102
      and (requested ~= "D" or math.abs(ele) <= 102) then
    invalidate()
    state, mode, cause = "T", requested, "W"
    resetAxis(yawAxis, "A")
    resetAxis(pitchAxis, requested == "D" and "A" or "N")
    sawMiddle = false
    enabledAt = now
    lastAccepted = nil
  end
  if state == "T" or state == "A" then
    axisAuthority(yawAxis, now, rud)
    if mode == "D" then axisAuthority(pitchAxis, now, ele) end
    if yawAxis.phase ~= "A" then value, fresh = 0, 0 end
    if pitchAxis.phase ~= "A" then pitch, pitchFresh = 0, 0 end
    if enabledAt ~= nil and elapsed(now, lastAccepted or enabledAt) >= 100 then
      fault("L")
    elseif leaseIssued ~= nil and elapsed(now, leaseIssued) >= 30 then
      withdraw()
      state, cause = "T", "E"
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
  local begin = string.match(line, "^DB3 ([0-9a-f]+)$")
  if begin and #begin == 8 and begin ~= "00000000" then
    if begin == session then return end
    session = begin
    sequence = 0
    manual("M", "S")
    invalidate()
    sawMiddle = false
    return
  end

  local token, genText, ticketText, seqText, validText, valueText, pitchValidText, pitchText = string.match(
    line, "^DS3 ([0-9a-f]+) (%d+) (%d+) (%d+) ([01]) ([%-]?%d+) ([01]) ([%-]?%d+)$")
  local gen, issuedTicket = number(genText, MAX_SEQUENCE), number(ticketText, MAX_SEQUENCE)
  local nextSequence, nextValue = number(seqText, MAX_SEQUENCE), tonumber(valueText)
  local nextPitch = tonumber(pitchText)
  if (state ~= "A" and state ~= "T") or token ~= session or gen ~= generation
      or not nextSequence or nextSequence <= sequence
      or (pending and nextSequence <= pending.sequence)
      or not nextValue or math.type(nextValue) ~= "integer"
      or nextValue < -MAX_VALUE or nextValue > MAX_VALUE or valueText ~= tostring(nextValue)
      or not nextPitch or math.type(nextPitch) ~= "integer"
      or nextPitch < -MAX_PITCH or nextPitch > MAX_PITCH or pitchText ~= tostring(nextPitch)
      or (pitchValidText == "0" and nextPitch ~= 0)
      or (validText == "0" and (nextValue ~= 0 or pitchValidText ~= "0" or nextPitch ~= 0))
      or (mode ~= "D" and (pitchValidText ~= "0" or nextPitch ~= 0)) then return end
  for _, issued in ipairs(tickets) do
    if issued.number == issuedTicket and issued.generation == generation
        and elapsed(now, issued.clock) < 30 then
      -- Only publish the newest validated command once the complete bounded
      -- batch has been consumed. A queue cannot create multiple heartbeats.
      pending = {sequence = nextSequence, value = nextValue,
        valid = validText == "1", pitch = nextPitch, pitchValid = pitchValidText == "1",
        clock = issued.clock, ticket = issued.number}
      return
    end
  end
end

local function readBatch(now)
  pending = nil
  for chunk = 1, 4 do
    local ok, data = pcall(serialRead, 96)
    if not ok or type(data) ~= "string" or #data > 96 then
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
        if #buffer >= 96 then
          buffer = ""
          dropping = true
          fault("O")
        else
          buffer = buffer .. c
        end
      end
    end
    if #data < 96 then return end
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
    local yawAllowed = allowAxis(yawAxis)
    local pitchAllowed = pending.pitchValid and allowAxis(pitchAxis)
    value, fresh = yawAllowed and pending.value or 0, yawAllowed and 1024 or 0
    pitch, pitchFresh = pitchAllowed and pending.pitch or 0, pitchAllowed and 1024 or 0
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

local function pilotSample(now, rud, ele)
  if lastPilot ~= nil and elapsed(now, lastPilot) < 10 then return end
  lastPilot = now
  -- Optional pre-mix/script-output observation; never native mixer/FC proof.
  -- Bounded to 96 bytes and 10 Hz, including failed optional reads/writes.
  pcall(function()
    local ail, thr = getValue("ail"), getValue("thr")
    for _, source in ipairs({ail, ele, thr, rud}) do
      if type(source) ~= "number" or math.type(source) ~= "integer"
          or source < -1024 or source > 1024 then return end
    end
    if ail == nil or ele == nil or thr == nil or rud == nil then return end
    serialWrite(string.format("AP1 %s %d %d %d %s%s%s%s%s%d%d %d %d %d %d %d %d\n",
      session or "00000000", generation, ticket, sequence, state, cause,
      mode, yawAxis.phase, pitchAxis.phase, fresh > 0 and 1 or 0, pitchFresh > 0 and 1 or 0,
      ail, ele, thr, rud, value, pitch))
  end)
end

local function status(now, rud, ele)
  if lastHello == nil or elapsed(now, lastHello) >= 100 then
    if not write("ARGOS_DISTANCE_STREAM_V3\n") then return end
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
    if write(string.format("DY3 %s %d %d %d %s %s %s %s %s\n", session or "00000000",
        generation, ticket, sequence, state, cause, mode, yawAxis.phase, pitchAxis.phase)) then
      lastStatus = now
      pilotSample(now, rud, ele)
    end
  end
end

local function run()
  local allowed, sc, rud, ele, sb, reason, detail = permitted()
  local clockOK, now = pcall(getTime)
  if not allowed then
    fault("G")
    blocked(reason, detail, clockOK and now or nil)
    return 0, 0, 0, 0, 0, 0
  end
  if type(serialRead) ~= "function" or type(serialWrite) ~= "function" then
    fault("I")
    blocked("serial_api", type(serialRead) .. ":" .. type(serialWrite), clockOK and now or nil)
    return 0, 0, 0, 0, 0, 0
  end
  if not clockOK or not validClock(now) then
    fault("C")
    blocked("clock", clockOK and now or "api_exception", nil)
    return 0, 0, 0, 0, 0, 0
  end
  if lastClock ~= nil and elapsed(now, lastClock) >= 1073741824 then
    fault("C")
    lastClock = now
    blocked("clock", "regressed", now)
    return 0, 0, 0, 0, 0, 0
  end
  blockedActive, lastBlockedAt, blockedCallbacks = false, nil, 0
  lastClock = now
  -- Expire and inspect pilot authority BEFORE reading delayed USB bytes.
  authority(now, sc, rud, ele, sb)
  readBatch(now)
  publish(now)
  status(now, rud, ele)
  -- Seq is a bounded native-gate marker; the full ACK sequence is on the wire.
  return value, fresh, sequence > 0 and 1024 or 0, heartbeat, pitch, pitchFresh
end

return {output = {"Val", "Fsh", "Seq", "Hbt", "Pit", "Psh"}, run = run}
