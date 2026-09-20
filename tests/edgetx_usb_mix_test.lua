-- Run from the repository root with Lua 5.2 or later:
--   lua tests/edgetx_usb_mix_test.lua
-- This checks scheduled callbacks in a restricted mock, not radio hardware or
-- the behavior of an EdgeTX mixer after Lua stops executing.

local scriptPath = arg[1] or "scripts/edgetx/ArgMix.lua"
local HELLO = "ARGOS_USB_MIX_BENCH_V1\n"
local SESSION = "abc012ef"
local testsRun = 0
local testsFailed = 0

local function equal(actual, expected, message)
  assert(actual == expected, (message or "unexpected result")
    .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
end

local function checkedCall(callback, ...)
  local arguments = table.pack(...)
  debug.sethook(function()
    error("callback exceeded its instruction budget", 0)
  end, "", 100000)
  local values = table.pack(pcall(callback, table.unpack(arguments, 1, arguments.n)))
  debug.sethook()
  assert(values[1], values[2])
  return table.unpack(values, 2, values.n)
end

local function readonly(values, name)
  return setmetatable({}, {
    __index = function(_, key)
      return values[key]
    end,
    __newindex = function(_, key)
      error("attempt to write " .. name .. "." .. tostring(key), 0)
    end,
  })
end

local function radio(options)
  options = options or {}
  local state = {
    now = options.now or 0, input = "", writes = {}, reads = 0,
    name = options.name or "ARGOS USB", moduleTypes = {0, 0},
    modelReads = 0, moduleReads = {0, 0}, infoMissing = false,
    moduleMissing = {}, infoError = false, moduleError = {},
    missingRead = false, missingWrite = false,
  }
  local allowed = {
    math = math, string = string, table = table,
    tonumber = tonumber, tostring = tostring, type = type,
    pairs = pairs, ipairs = ipairs, pcall = pcall,
    getTime = function() return state.now end,
    model = readonly({
      getInfo = function()
        state.modelReads = state.modelReads + 1
        if state.infoError then error("mock model info error") end
        if state.infoMissing then return nil end
        return readonly({name = state.name}, "model information")
      end,
      getModule = function(index)
        assert(index == 0 or index == 1, "unexpected RF module index")
        state.moduleReads[index + 1] = state.moduleReads[index + 1] + 1
        if state.moduleError[index + 1] then error("mock RF module error") end
        if state.moduleMissing[index + 1] then return nil end
        return readonly({Type = state.moduleTypes[index + 1]}, "RF module")
      end,
    }, "model"),
    serialRead = function(count)
      state.reads = state.reads + 1
      assert(type(count) == "number" and count >= 1 and count <= 256,
        "serial read must request a bounded number of bytes")
      local value = state.input:sub(1, count)
      state.input = state.input:sub(count + 1)
      return value
    end,
    serialWrite = function(value)
      assert(type(value) == "string" and #value <= 256,
        "serial writes must be bounded strings")
      state.writes[#state.writes + 1] = value
    end,
  }
  local environment = setmetatable({}, {
    __index = function(_, key)
      if (key == "serialRead" and state.missingRead)
          or (key == "serialWrite" and state.missingWrite) then return nil end
      assert(allowed[key] ~= nil, "script accessed an undeclared capability: " .. tostring(key))
      return allowed[key]
    end,
    __newindex = function(_, key)
      error("script wrote a global: " .. tostring(key), 0)
    end,
  })
  state.script = checkedCall(assert(loadfile(scriptPath, "t", environment)))
  assert(type(state.script) == "table" and type(state.script.run) == "function")
  if state.script.init then checkedCall(state.script.init) end

  function state:step(input, time)
    self.input = self.input .. (input or "")
    if time then self.now = time end
    self.output = table.pack(checkedCall(self.script.run))
    return table.unpack(self.output, 1, self.output.n)
  end

  function state:drain(input)
    self.input = self.input .. input
    for _ = 1, 1000 do
      if #self.input == 0 then return end
      local before = #self.input
      self:step()
      assert(#self.input < before, "serial reader made no progress")
    end
    error("serial queue did not drain within the callback limit")
  end

  function state:expect(value, fresh, sequence)
    equal(self.output.n, 3, "mixer must return exactly three values")
    equal(self.output[1], value, "value output")
    equal(self.output[2], fresh, "freshness output")
    equal(self.output[3], sequence, "sequence output")
  end

  function state:countWrites(prefix)
    local count = 0
    for _, value in ipairs(self.writes) do
      if value:sub(1, #prefix) == prefix then count = count + 1 end
    end
    return count
  end

  function state:begin(session)
    session = session or SESSION
    self:drain("ARGOS_MIX_BEGIN " .. session .. "\n")
    equal(self.writes[#self.writes], "ARGOS_MIX_READY " .. session .. "\n")
    self:expect(0, 0, 0)
  end

  function state:set(sequence, value, time, session)
    session = session or SESSION
    self:step("ARGOS_MIX_SET " .. session .. " " .. sequence .. " " .. value .. "\n", time)
  end

  return state
end

local function test(name, callback)
  testsRun = testsRun + 1
  local ok, message = pcall(callback)
  if ok then
    print("ok - " .. name)
  else
    testsFailed = testsFailed + 1
    print("not ok - " .. name .. ": " .. tostring(message))
  end
end

test("cold bench announces itself with neutral outputs", function()
  local r = radio()
  equal(table.concat(r.script.output, ","), "Val,Fsh,Seq", "named mixer outputs")
  r:step()
  r:expect(0, 0, 0)
  equal(r.writes[1], HELLO)
  assert(r.moduleReads[1] > 0 and r.moduleReads[2] > 0,
    "both RF modules must be checked")
  r:step(nil, 49)
  equal(#r.writes, 1)
  r:step(nil, 50)
  equal(r.writes[2], HELLO)
end)

test("wrong model name blocks serial activity and all outputs", function()
  for _, name in ipairs({"POCKET", "argos usb", "ARGOS USB "}) do
    local r = radio({name = name})
    r:step("ARGOS_MIX_BEGIN " .. SESSION .. "\n")
    r:expect(0, 0, 0)
    equal(r.reads, 0)
    equal(#r.writes, 0)
  end
end)

test("each enabled RF module blocks the diagnostic", function()
  for module = 1, 2 do
    for _, moduleType in ipairs({1, 5, -1, "0"}) do
      local r = radio()
      r.moduleTypes[module] = moduleType
      r:step()
      r:expect(0, 0, 0)
      equal(r.reads, 0)
      equal(#r.writes, 0)
    end
  end
end)

test("missing model/module information and getter errors fail closed", function()
  local mutations = {
    function(r) r.infoMissing = true end,
    function(r) r.infoError = true end,
    function(r) r.moduleMissing[1] = true end,
    function(r) r.moduleMissing[2] = true end,
    function(r) r.moduleError[1] = true end,
    function(r) r.moduleError[2] = true end,
    function(r) r.moduleTypes[1] = nil end,
    function(r) r.moduleTypes[2] = nil end,
  }
  for _, mutate in ipairs(mutations) do
    local r = radio()
    mutate(r)
    r:step()
    r:expect(0, 0, 0)
    equal(r.reads, 0)
    equal(#r.writes, 0)
  end
end)

test("a session is required and partial frames cannot produce output", function()
  local r = radio()
  r:set(1, 256)
  r:expect(0, 0, 0)
  equal(r:countWrites("ARGOS_MIX_ACK"), 0)
  r:step("ARGOS_MIX_BEGIN abc0")
  r:expect(0, 0, 0)
  r:step("12ef\nARGOS_MIX_SET abc012ef 1 ")
  r:expect(0, 0, 0)
  r:step("256")
  r:expect(0, 0, 0)
  r:step("\n")
  r:expect(256, 1024, 1)
  equal(r.writes[#r.writes], "ARGOS_MIX_ACK abc012ef 1 256\n")
end)

test("fixed bench steps drive outputs and acknowledgements in order", function()
  local r = radio()
  r:begin()
  for sequence, value in ipairs({0, 256, 0, -256, 0}) do
    r:set(sequence, value, sequence)
    r:expect(value, 1024, sequence)
    equal(r.writes[#r.writes], "ARGOS_MIX_ACK " .. SESSION .. " "
      .. sequence .. " " .. value .. "\n")
  end
  equal(r:countWrites("ARGOS_MIX_ACK"), 5)
end)

test("only strictly newer sequence numbers in the admitted range renew output", function()
  local r = radio()
  r:begin()
  r:set(3, 256, 0)
  for _, sequence in ipairs({3, 2, 0, 121, -1, "04", "+4", "4.0", "4e0"}) do
    r:set(sequence, -256, 10)
    r:expect(256, 1024, 3)
  end
  equal(r:countWrites("ARGOS_MIX_ACK"), 1)
  r:step(nil, 30)
  r:expect(0, 0, 3)
  r:set(120, -256, 31)
  r:expect(-256, 1024, 120)
  equal(r:countWrites("ARGOS_MIX_ACK"), 2)
end)

test("other sessions, malformed numbers and extreme values are rejected", function()
  local r = radio()
  r:begin()
  r:set(1, 256, 0)
  for _, value in ipairs({"257", "-257", "1024", "-1024", "9999999999999999999999",
      "1", "-1", "00", "+256", "256.0", "2.56e2", "NaN", "inf", "-0"}) do
    r:set(2, value, 20)
    r:expect(256, 1024, 1)
  end
  r:set(2, -256, 20, "abc012e0")
  r:expect(256, 1024, 1)
  equal(r:countWrites("ARGOS_MIX_ACK"), 1)
  r:step(nil, 30)
  r:expect(0, 0, 1)
end)

test("invalid session starts cannot replace an active session", function()
  local r = radio()
  r:begin()
  r:set(1, 256)
  for _, session in ipairs({"ABC012EF", "abc012e", "abc012ef0", "abc012eg",
      "abc0 2ef", "abc012ef ", "abc012ef\r"}) do
    r:drain("ARGOS_MIX_BEGIN " .. session .. "\n")
    r:expect(256, 1024, 1)
  end
  equal(r:countWrites("ARGOS_MIX_READY"), 1)
  r:set(2, -256)
  r:expect(-256, 1024, 2)
end)

test("the running callback expires output after 300 ms and reports idle once", function()
  local r = radio()
  r:begin()
  r:set(1, -256, 100)
  r:step(nil, 129)
  r:expect(-256, 1024, 1)
  r:step(nil, 130)
  r:expect(0, 0, 1)
  equal(r.writes[#r.writes], "ARGOS_MIX_IDLE " .. SESSION .. " 1\n")
  r:step(nil, 200)
  equal(r:countWrites("ARGOS_MIX_IDLE"), 1)
  r:set(2, 0, 201)
  r:expect(0, 1024, 2)
  r:step(nil, 231)
  r:expect(0, 0, 2)
  equal(r:countWrites("ARGOS_MIX_IDLE"), 2)
end)

test("an accepted new session resets values and invalidates old-session traffic", function()
  local r = radio()
  r:begin()
  r:set(1, 256)
  r:begin("1234abcd")
  r:set(2, -256)
  r:expect(0, 0, 0)
  r:set(1, -256, 1, "1234abcd")
  r:expect(-256, 1024, 1)
end)

test("script reload starts neutral and cannot reuse a previous local session", function()
  local before = radio()
  before:begin()
  before:set(1, 256)
  local after = radio()
  after:set(2, 256)
  after:expect(0, 0, 0)
  equal(after:countWrites("ARGOS_MIX_ACK"), 0)
  after:begin("1234abcd")
  after:set(1, -256, 0, "1234abcd")
  after:expect(-256, 1024, 1)
end)

test("RF activation immediately clears output and requires a session after recovery", function()
  local r = radio()
  r:begin()
  r:set(1, 256, 0)
  local reads, writes = r.reads, #r.writes
  r.moduleTypes[1] = 5
  r:step(nil, 1)
  r:expect(0, 0, 0)
  equal(r.reads, reads)
  equal(#r.writes, writes)
  r.moduleTypes[1] = 0
  r:set(2, 256, 2)
  r:expect(0, 0, 0)
  equal(r.writes[#r.writes], HELLO)
  r:begin("1234abcd")
  r:set(1, -256, 3, "1234abcd")
  r:expect(-256, 1024, 1)
end)

test("a guard failure discards a partially received frame", function()
  local r = radio()
  r:begin()
  r:step("ARGOS_MIX_SET " .. SESSION .. " 1 ")
  r.name = "POCKET"
  r:step()
  r:expect(0, 0, 0)
  r.name = "ARGOS USB"
  r:step("256\n")
  r:expect(0, 0, 0)
  equal(r:countWrites("ARGOS_MIX_ACK"), 0)
  r:begin()
  r:set(1, -256)
  r:expect(-256, 1024, 1)
end)

test("unavailable serial APIs clear active output without serial access", function()
  for _, missing in ipairs({"missingRead", "missingWrite"}) do
    local r = radio()
    r:begin()
    r:set(1, 256)
    local reads, writes = r.reads, #r.writes
    r[missing] = true
    r:step()
    r:expect(0, 0, 0)
    equal(r.reads, reads)
    equal(#r.writes, writes)
  end
end)

test("oversized and noisy input cannot smuggle an accepted suffix", function()
  local r = radio()
  r:begin()
  r:drain(string.rep("x", 4096))
  r:drain("ARGOS_MIX_SET " .. SESSION .. " 1 256\n")
  r:expect(0, 0, 0)
  equal(r:countWrites("ARGOS_MIX_ACK"), 0)
  r:drain("noise\n\0ARGOS_MIX_SET " .. SESSION .. " 1 256\n")
  r:expect(0, 0, 0)
  r:set(1, -256)
  r:expect(-256, 1024, 1)
end)

test("lease expiry works across the radio clock wrapping", function()
  local r = radio({now = 4294967286})
  r:begin()
  r:set(1, 256)
  r:step(nil, 19)
  r:expect(256, 1024, 1)
  r:step(nil, 20)
  r:expect(0, 0, 1)
end)

assert(testsFailed == 0, testsFailed .. " of " .. testsRun .. " EdgeTX mixer tests failed")
print("Passed " .. testsRun .. " EdgeTX mixer tests (scheduled callbacks; RF-off mock only).")
