-- Run from the repository root with Lua 5.2 or later:
--   lua tests/edgetx_usb_hold_test.lua
-- ArgHld deliberately retains nonzero outputs while callbacks continue.
-- These tests do not simulate a Lua crash or validate native channel gating.

local scriptPath = arg[1] or "scripts/edgetx/ArgHld.lua"
local VALUE = 0
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

local function radio(options)
  options = options or {}
  local state = {
    now = options.now or 0, name = options.name or "ARGOS USB",
    moduleTypes = {0, 0}, moduleReads = {0, 0}, infoMissing = false,
    infoError = false, moduleMissing = {}, moduleError = {},
    clockReads = 0, violations = {},
  }
  local function forbidden(message)
    state.violations[#state.violations + 1] = message
    error(message, 0)
  end
  local function readonly(values, name, strict)
    return setmetatable({}, {
      __index = function(_, key)
        if strict and values[key] == nil then
          forbidden("undeclared API: " .. name .. "." .. tostring(key))
        end
        return values[key]
      end,
      __newindex = function(_, key)
        forbidden("attempt to write " .. name .. "." .. tostring(key))
      end,
    })
  end
  local environment = readonly({
    VALUE = VALUE, math = math, type = type, pcall = pcall,
    getTime = function()
      state.clockReads = state.clockReads + 1
      return state.now
    end,
    model = readonly({
      getInfo = function()
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
    }, "model", true),
  }, "global", true)

  state.script = checkedCall(assert(loadfile(scriptPath, "t", environment)))
  assert(type(state.script) == "table" and type(state.script.run) == "function")
  if state.script.init then checkedCall(state.script.init) end
  equal(#state.violations, 0, "script initialization capability violations")

  function state:step(polarity, time)
    if time then self.now = time end
    self.output = table.pack(checkedCall(self.script.run, polarity))
    equal(#self.violations, 0, "script capability violations")
    return table.unpack(self.output, 1, self.output.n)
  end

  function state:expect(value, fresh, sequence, heartbeat)
    equal(self.output.n, 4, "fixture must return exactly four outputs")
    equal(self.output[1], value, "value output")
    equal(self.output[2], fresh, "freshness output")
    equal(self.output[3], sequence, "sequence output")
    if heartbeat ~= nil then
      equal(self.output[4], heartbeat, "heartbeat output")
    else
      assert(self.output[4] == -1024 or self.output[4] == 1024,
        "exercise heartbeat must have a full-scale polarity")
    end
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

test("fixture exposes the compatible outputs and bounded polarity selector", function()
  local r = radio()
  equal(table.concat(r.script.output, ","), "Val,Fsh,Seq,Hbt")
  equal(#r.script.input, 1)
  local input = r.script.input[1]
  equal(input[1], "Pol")
  equal(input[2], VALUE)
  equal(input[3], 0)
  equal(input[4], 1)
  equal(input[5], 1)
  equal(r.clockReads, 0, "loading must not start the ten-second clock")
end)

test("both RF modules are checked before the exercise can start", function()
  local r = radio()
  r:step(1)
  r:expect(256, 1024, 1)
  assert(r.moduleReads[1] > 0 and r.moduleReads[2] > 0)
  equal(r.clockReads, 1)
end)

test("wrong model names return zero without reading the clock", function()
  for _, name in ipairs({"POCKET", "argos usb", "ARGOS USB "}) do
    local r = radio({name = name})
    r:step(1, 1000)
    r:expect(0, 0, 0, 0)
    equal(r.clockReads, 0)
  end
end)

test("enabled or invalid RF module types block the exercise before time access", function()
  for module = 1, 2 do
    for _, moduleType in ipairs({1, 5, -1, "0", false}) do
      local r = radio()
      r.moduleTypes[module] = moduleType
      r:step(1)
      r:expect(0, 0, 0, 0)
      equal(r.clockReads, 0)
    end
  end
end)

test("missing or erroring model getters return zero without reading the clock", function()
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
    r:step(1)
    r:expect(0, 0, 0, 0)
    equal(r.clockReads, 0)
  end
end)

test("only numeric zero or one is an admitted polarity", function()
  local invalid = table.pack(nil, false, true, "0", "1", -1, 2, 0.5, 0/0, math.huge, {})
  for index = 1, invalid.n do
    local r = radio()
    r:step(invalid[index])
    r:expect(0, 0, 0, 0)
    equal(r.clockReads, 0)
  end
end)

test("startup alternates heartbeat at 100 ms boundaries while values remain nonzero", function()
  for _, polarity in ipairs({0, 1}) do
    local r = radio({now = 1000})
    r:step(polarity)
    r:expect(256, 1024, 1)
    local first = r.output[4]
    r:step(polarity, 1009)
    r:expect(256, 1024, 1, first)
    r:step(polarity, 1010)
    r:expect(256, 1024, 1, -first)
    r:step(polarity, 1019)
    r:expect(256, 1024, 1, -first)
    r:step(polarity, 1020)
    r:expect(256, 1024, 1, first)
    r:step(polarity, 1990)
    r:expect(256, 1024, 1, -first)
    r:step(polarity, 1999)
    r:expect(256, 1024, 1, -first)
  end
end)

test("after ten seconds both selected polarities hold nonzero values indefinitely", function()
  for _, polarity in ipairs({0, 1}) do
    local r = radio({now = 1000})
    r:step(polarity)
    local fixed = polarity == 0 and -1024 or 1024
    for _, time in ipairs({2000, 2001, 2010, 5000, 1000000}) do
      r:step(polarity, time)
      r:expect(256, 1024, 1, fixed)
    end
  end
end)

test("changing polarity restarts the full ten-second exercise", function()
  for _, polarity in ipairs({0, 1}) do
    local r = radio()
    r:step(polarity, 0)
    r:step(polarity, 1000)
    local changed = 1 - polarity
    r:step(changed, 2000)
    r:expect(256, 1024, 1)
    local first = r.output[4]
    r:step(changed, 2010)
    r:expect(256, 1024, 1, -first)
    r:step(changed, 2999)
    r:expect(256, 1024, 1, -first)
    r:step(changed, 3000)
    r:expect(256, 1024, 1, changed == 0 and -1024 or 1024)
  end
end)

test("an invalid polarity clears a running fixture and restarts its clock", function()
  local r = radio()
  r:step(1, 0)
  r:step(1, 1000)
  local clocks = r.clockReads
  r:step(nil, 1100)
  r:expect(0, 0, 0, 0)
  equal(r.clockReads, clocks)
  r:step(1, 2000)
  local first = r.output[4]
  r:step(1, 2010)
  r:expect(256, 1024, 1, -first)
  r:step(1, 3000)
  r:expect(256, 1024, 1, 1024)
end)

test("RF or model guard loss resets all outputs and the exercise on re-entry", function()
  local cases = {
    {
      block = function(r) r.name = "POCKET" end,
      restore = function(r) r.name = "ARGOS USB" end,
    },
    {
      block = function(r) r.moduleTypes[1] = 5 end,
      restore = function(r) r.moduleTypes[1] = 0 end,
    },
    {
      block = function(r) r.moduleTypes[2] = 5 end,
      restore = function(r) r.moduleTypes[2] = 0 end,
    },
  }
  for _, case in ipairs(cases) do
    local r = radio()
    r:step(0, 0)
    r:step(0, 1000)
    local clocks = r.clockReads
    case.block(r)
    r:step(0, 1100)
    r:expect(0, 0, 0, 0)
    equal(r.clockReads, clocks)
    case.restore(r)
    r:step(0, 2000)
    local first = r.output[4]
    r:step(0, 2010)
    r:expect(256, 1024, 1, -first)
    r:step(0, 3000)
    r:expect(256, 1024, 1, -1024)
  end
end)

test("a fresh script instance restarts instead of inheriting retained values", function()
  local old = radio()
  old:step(0, 0)
  old:step(0, 1000)
  old:expect(256, 1024, 1, -1024)
  local fresh = radio({now = 2000})
  fresh:step(0)
  local first = fresh.output[4]
  fresh:step(0, 2010)
  fresh:expect(256, 1024, 1, -first)
end)

test("startup and held polarity survive the unsigned radio clock wrapping", function()
  for _, polarity in ipairs({0, 1}) do
    local r = radio({now = 4294967291})
    r:step(polarity)
    local first = r.output[4]
    r:step(polarity, 4)
    r:expect(256, 1024, 1, first)
    r:step(polarity, 5)
    r:expect(256, 1024, 1, -first)
    r:step(polarity, 994)
    r:expect(256, 1024, 1, -first)
    r:step(polarity, 995)
    r:expect(256, 1024, 1, polarity == 0 and -1024 or 1024)
    r:step(polarity, 10000)
    r:expect(256, 1024, 1, polarity == 0 and -1024 or 1024)
  end
end)

test("once held, a later clock cycle cannot restart heartbeat activity", function()
  for _, polarity in ipairs({0, 1}) do
    local r = radio({now = 1000})
    r:step(polarity)
    r:step(polarity, 2000)
    local fixed = polarity == 0 and -1024 or 1024
    r:expect(256, 1024, 1, fixed)
    r:step(polarity, 4294967295)
    r:expect(256, 1024, 1, fixed)
    r:step(polarity, 1000)
    r:expect(256, 1024, 1, fixed)
    r:step(polarity, 1010)
    r:expect(256, 1024, 1, fixed)
  end
end)

assert(testsFailed == 0, testsFailed .. " of " .. testsRun .. " EdgeTX hold tests failed")
print("Passed " .. testsRun .. " EdgeTX hold tests (scheduled fixture; no native gate validation).")
