-- RF-OFF FAULT FIXTURE: no aircraft, only unused CH32 in model "ARGOS USB".
-- Ten seconds of synthetic heartbeat, then deliberately held nonzero outputs.
-- This never expires Val/Fsh: the separately configured NATIVE gate must act.
-- Pol=0 holds Hbt negative, Pol=1 holds it positive. Changing Pol restarts.
-- Install in SCRIPTS/MIXES; restore ArgMix in the same slot after the test.
local started = nil
local polarity = nil
local held = false

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

local function run(pol)
  if not permitted() or (pol ~= 0 and pol ~= 1) then
    started = nil
    polarity = nil
    held = false
    return 0, 0, 0, 0
  end

  local now = getTime()
  if started == nil or polarity ~= pol then
    started = now
    polarity = pol
    held = false
  end
  local age = (now - started) % 4294967296
  if age >= 1000 then held = true end

  local heartbeat
  if held then
    heartbeat = pol == 1 and 1024 or -1024
  else
    heartbeat = math.floor(age / 10) % 2 == 0 and 1024 or -1024
  end
  -- Deliberately held values model a stalled producer while EdgeTX keeps mixing.
  -- There is no serial I/O, model write, channel write or native-gate decision.
  return 256, 1024, 1, heartbeat
end

return {
  input = { { "Pol", VALUE, 0, 1, 1 } },
  output = { "Val", "Fsh", "Seq", "Hbt" },
  run = run,
}
