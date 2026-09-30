-- lua tests/edgetx_yaw_stream_test.lua (Lua 5.3+); no radio or model mutations.
local radio = dofile("tests/edgetx_yaw_stream_fixture.lua")
local count, failed = 0, 0
local function eq(actual, expected)
  assert(actual == expected, "expected " .. tostring(expected) .. ", got " .. tostring(actual))
end
local function test(name, fn)
  count = count + 1
  local ok, error = pcall(fn)
  if ok then print("ok - " .. name) else
    failed = failed + 1
    print("not ok - " .. name .. ": " .. tostring(error))
  end
end
local function active()
  local r = radio()
  r:begin()
  r:arm()
  r:set(1, 80)
  r:expect(80, 1024, 1024)
  return r
end

test("startup and BEGIN while SC up cannot enable", function()
  local r = radio({sc = -1024})
  r:step()
  eq(r.writes[1], "ARGOS_YAW_STREAM_V1\n")
  eq(r:latestStatus().session, "00000000")
  eq(r:latestStatus().state, "M")
  r:begin()
  r:set(1, 128)
  r:expect(0, 0, 0)
  r:step(nil, 50)
  eq(r:latestStatus().state, "M")
  r:arm()
  r:set(1, 128)
  r:expect(128, 1024)
end)

test("centered target zero remains valid; unavailable target restores manual", function()
  local r = active()
  r:set(2, 0)
  r:expect(0, 1024)
  r:set(3, 0, false)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "T")
  eq(r:latestStatus().sequence, 3)
  r:set(4, -40)
  r:expect(-40, 1024)
end)

test("Mode 2 small yaw jitter and short excursions preserve assistance", function()
  local r = active()
  r.rud = 240
  r:set(2, 70, true, 10)
  r:expect(70, 1024)
  r.rud = -256 -- exactly 25% does not trigger the strict native threshold.
  r:set(3, 70, true, 20)
  r.rud = 350
  r:set(4, 70, true, 25)
  r:set(5, 70, true, 44) -- nineteen ticks outside the threshold.
  r:expect(70, 1024)
  r.rud = 200
  r:set(6, 70, true, 45)
  r:expect(70, 1024)
end)

test("deliberate yaw held above 25% for 200ms latches manual", function()
  local r = active()
  r.rud = -257
  r:set(2, 70, true, 10)
  r:set(3, 70, true, 29)
  r:expect(70, 1024)
  r:set(4, 70, true, 30)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "F")
  r.rud = 0
  r:set(5, 70, true, 40)
  r:expect(0, 0, 0)
  r:arm()
  r:set(5, 70)
  r:expect(70, 1024)
end)

test("emergency yaw and a native between-callback latch both withdraw", function()
  for _, case in ipairs({"stick", "native"}) do
    local r = active()
    if case == "stick" then r.rud = 513 else r.native = true end
    r:step(nil, 1)
    r:expect(0, 0, 0)
    eq(r:latestStatus().state, "F")
    r.rud, r.native = 0, false
    r:set(2, 80, true, 10)
    r:expect(0, 0, 0)
  end
end)

test("exact 50% boundary uses the deliberate hold, not emergency path", function()
  local r = active()
  r.rud = 512
  r:set(2, 60, true, 10)
  r:expect(60, 1024)
  r:set(3, 60, true, 30)
  r:expect(0, 0, 0)
end)

test("SC cycle changes generation and rejects the old enabled epoch", function()
  local r = active()
  local old = r:latestStatus()
  r.sc = 0
  r:step(nil, 10)
  r:expect(0, 0, 0)
  r.sc = -1024
  r:step(nil, 11)
  assert(r:latestStatus().generation ~= old.generation)
  r:set(2, 128, true, 12, old)
  r:expect(0, 0, 0)
  r:set(2, -64, true, 13)
  r:expect(-64, 1024)
end)

test("short lease expiry withdraws and a new ticket can recover", function()
  local r = active()
  r:step(nil, 30)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "T")
  r:set(2, 32, true, 31)
  r:expect(32, 1024)
end)

test("one second of silence latches and queued SET cannot revive it", function()
  local r = active()
  r:step(nil, 90)
  r:set(2, 128, true, 100)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "F")
  r:arm()
  r:set(2, -80, true, 101)
  r:expect(-80, 1024)
end)

test("late delivery keeps ticket issue expiry instead of renewing on receipt", function()
  local r = radio()
  r:begin()
  local old = r:arm()
  r:set(1, 128, true, 29, old)
  r:expect(128, 1024)
  r:step(nil, 30)
  r:expect(0, 0, 0)
  r:set(2, 128, true, 31, old)
  r:expect(0, 0, 0)
end)

test("fresh sequence cannot refresh an expired ticket or frozen producer", function()
  local r = active()
  local old = r:latestStatus()
  r:set(2, 60, true, 20, old)
  r:expect(60, 1024)
  r:set(3, 60, true, 30, old)
  r:expect(0, 0, 0)
  r:set(4, 60, true, 120, old)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "F")
end)

test("duplicate sequence does not toggle heartbeat or renew acceptance clock", function()
  local r = active()
  r:set(1, 128, true, 10)
  r:expect(80, 1024, 1024)
  r:set(1, 128, true, 100)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "F")
end)

test("one bounded batch publishes newest sequence once", function()
  local r = active()
  local input = r:command(2, 60) .. r:command(4, -80) .. r:command(3, 120)
  r:step(input, 10)
  r:expect(-80, 1024, -1024)
  eq(r:latestStatus().sequence, 4)
  eq(r.output[3], 1024)
end)

test("full 256-byte callback budget withdraws rather than playing a queue", function()
  local r = active()
  r:step(string.rep("#\n", 128) .. r:command(2, 128), 10)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "F")
  eq(r.lastReads, 4)
  r:step(nil, 11)
  r:expect(0, 0, 0)
  r:arm()
  r:set(2, 64)
  r:expect(64, 1024)
end)

test("oversized line invalidates even a valid command earlier in the batch", function()
  local r = active()
  r:step(r:command(2, 128) .. string.rep("x", 65) .. "\n", 10)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "F")
  r:step(r:command(3, 128), 11)
  r:expect(0, 0, 0)
end)

test("new host session after restart requires SC cycle; repeated BEGIN does not reset", function()
  local r = active()
  local old = r:latestStatus()
  r:begin()
  r:expect(80, 1024)
  r:begin("1234abcd")
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "M")
  r:set(2, 128, true, 10, old)
  r:set(1, 128)
  r:expect(0, 0, 0)
  r:arm()
  r:set(1, 64)
  r:expect(64, 1024)
end)

test("ticket lifetime survives getTime negative-to-zero wrap", function()
  local r = radio({now = -16})
  r:begin()
  r:arm()
  r:set(1, 40)
  r:step(nil, 13) -- age 29 ticks.
  r:expect(40, 1024)
  r:step(nil, 14) -- exactly 30.
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "T")
end)

test("signed 32-bit clock rollover and large integer sequence stay exact", function()
  local r = radio({now = 2147483640})
  r:begin()
  r:arm()
  r:set(16777217, 40) -- above the exact single-float integer range.
  eq(r:latestStatus().sequence, 16777217)
  r:step(nil, -2147483627) -- age 29 ticks across the signed boundary.
  r:expect(40, 1024)
  r:step(nil, -2147483626)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "T")
  r:set(2147483647, -40)
  r:expect(-40, 1024)
  eq(r:latestStatus().sequence, 2147483647)
end)

test("backwards radio clock is a latched fault", function()
  local r = active()
  r:set(2, 80, true, 10)
  r:step(nil, 9)
  r:expect(0, 0, 0)
  eq(r:latestStatus().state, "F")
end)

test("model, module, crash flip and native input guards fail closed", function()
  local failures = {
    function(r) r.name = "ARGOS VIS" end,
    function(r) r.internal.Type = 0 end,
    function(r) r.internal.channelsCount = 8 end,
    function(r) r.internal.firstChannel = 1 end,
    function(r) r.external.Type = 5 end,
    function(r) r.flip = 1024 end,
    function(r) r.native = nil end,
    function(r) r.native = 0 end,
    function(r) r.rud = 0 / 0 end,
    function(r) r.sc = 12 end,
    function(r) r.modelError = true end,
    function(r) r.modelMissing = true end,
    function(r) r.internal = nil end,
    function(r) r.missingRead = true end,
    function(r) r.missingWrite = true end,
  }
  for _, fail in ipairs(failures) do
    local r = active()
    fail(r)
    r:step(nil, 10)
    r:expect(0, 0, 0)
    eq(r.lastReads, 0)
  end
end)

test("USB read/write errors require rearm after recovery", function()
  for _, field in ipairs({"readError", "writeError", "badRead"}) do
    local r = active()
    r[field] = true
    r:step(nil, 10)
    r:expect(0, 0, 0)
    r[field] = false
    r:step("#\n", 11)
    eq(r:latestStatus().state, "F")
    r:arm()
    r:set(2, -50)
    r:expect(-50, 1024)
  end
end)

test("noncanonical, invalid and out-of-range commands cannot acquire authority", function()
  local r = active()
  r:set(2, 0, false)
  local s = r:latestStatus()
  local prefix = string.format("AS1 %s %d %d ", s.session, s.generation, s.ticket)
  for _, tail in ipairs({"03 1 128", "3 1 -0", "3 0 1", "3 2 0", "0 1 1",
      "2147483648 1 1", "3 1 129", "3 1 -129", "3 1 1.5", "3 1 01"}) do
    r:step(prefix .. tail .. "\n")
    r:expect(0, 0, 0)
  end
  r:step("AB1 00000000\n")
  eq(r:latestStatus().session, "abc012ef")
end)

test("sustained unavailable-target commands stay manual without artificial session cap", function()
  local r = active()
  for i = 2, 1801 do
    r:set(i, 0, false, (i - 1) * 10)
    r:expect(0, 0, 0)
  end
  eq(r:latestStatus().state, "T")
  r:set(1802, -30, true, 18010)
  r:expect(-30, 1024)
end)

print(string.format("%d tests, %d failures", count, failed))
os.exit(failed == 0 and 0 or 1)
