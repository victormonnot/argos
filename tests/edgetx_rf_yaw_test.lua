-- Run from the repository root with Lua 5.2 or later:
--   lua tests/edgetx_rf_yaw_test.lua
-- Restricted callback tests only: no radio, receiver, FC or native gate proof.

local scriptPath = arg[1] or "scripts/edgetx/ArgRF.lua"
local HELLO = "ARGOS_RF_YAW_BENCH_V1\n"
local SESSION = "abc012ef"
local testsRun, testsFailed = 0, 0

local function equal(actual, expected, message)
  assert(actual == expected, (message or "unexpected result")
    .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
end

local function checkedCall(callback, ...)
  local arguments = table.pack(...)
  debug.sethook(function() error("callback exceeded its instruction budget", 0) end,
    "", 100000)
  local results = table.pack(pcall(callback, table.unpack(arguments, 1, arguments.n)))
  debug.sethook()
  assert(results[1], results[2])
  return table.unpack(results, 2, results.n)
end

local function radio()
  local r = {
    name = "ARGOS RF", now = 0, input = "", writes = {}, reads = 0,
    clockReads = 0, moduleReads = {0, 0}, outputReads = {}, violations = {},
    modules = {{Type = 5, firstChannel = 0, channelsCount = 16}, {Type = 0}},
    outputs = {[2] = -1024, [4] = -1024, [6] = -1024},
    missing = {}, moduleMissing = {}, moduleError = {}, outputError = {},
  }
  local function forbidden(message)
    r.violations[#r.violations + 1] = message
    error(message, 0)
  end
  local function readonly(values, name, strict)
    return setmetatable({}, {
      __index = function(_, key)
        if strict and values[key] == nil then forbidden("undeclared " .. name .. "." .. key) end
        return values[key]
      end,
      __newindex = function(_, key) forbidden("write to " .. name .. "." .. key) end,
    })
  end
  local allowed = {
    pcall = pcall, type = type, string = string, tonumber = tonumber, tostring = tostring,
    getTime = function() r.clockReads = r.clockReads + 1; return r.now end,
    model = readonly({
      getInfo = function()
        if r.infoError then error("mock model info error") end
        if r.infoMissing then return nil end
        return readonly({name = r.name}, "model information")
      end,
      getModule = function(index)
        assert(index == 0 or index == 1, "unexpected module index")
        r.moduleReads[index + 1] = r.moduleReads[index + 1] + 1
        if r.moduleError[index + 1] then error("mock module error") end
        if r.moduleMissing[index + 1] then return nil end
        return readonly(r.modules[index + 1], "module information")
      end,
    }, "model", true),
    getOutputValue = function(index)
      if index ~= 2 and index ~= 4 and index ~= 6 then
        forbidden("unexpected final output index " .. tostring(index))
      end
      r.outputReads[index] = (r.outputReads[index] or 0) + 1
      if r.outputError[index] then error("mock output error") end
      return r.outputs[index]
    end,
    serialRead = function(count)
      equal(count, 64, "fixed per-callback serial budget")
      r.reads = r.reads + 1
      local value = r.input:sub(1, count)
      r.input = r.input:sub(count + 1)
      return value
    end,
    serialWrite = function(value)
      assert(type(value) == "string" and #value <= 64, "bounded serial writes")
      assert(value == HELLO or value:match("^ARGOS_RF_READY [0-9a-f]+\n$")
        or value:match("^ARGOS_RF_ACK [0-9a-f]+ %d+ [%-]?%d+\n$")
        or value:match("^ARGOS_RF_IDLE [0-9a-f]+ %d+\n$"), "unexpected wire output")
      r.writes[#r.writes + 1] = value
    end,
  }
  local environment = setmetatable({}, {
    __index = function(_, key)
      if r.missing[key] then return nil end
      if allowed[key] == nil then forbidden("undeclared global " .. tostring(key)) end
      return allowed[key]
    end,
    __newindex = function(_, key) forbidden("global write " .. tostring(key)) end,
  })
  r.script = checkedCall(assert(loadfile(scriptPath, "t", environment)))
  equal(#r.violations, 0)
  function r:step(input, time)
    self.input = self.input .. (input or "")
    if time then self.now = time end
    local reads = self.reads
    self.output = table.pack(checkedCall(self.script.run))
    equal(#self.violations, 0, "forbidden capabilities attempted")
    assert(self.reads - reads <= 1, "more than one serial read per callback")
  end
  function r:drain(input)
    self.input = self.input .. input
    for _ = 1, 1000 do
      if self.input == "" then return end
      local before = #self.input
      self:step()
      assert(#self.input < before, "input did not make progress")
    end
    error("input did not drain within callback limit")
  end
  function r:expect(value, fresh, sequence, heartbeat)
    equal(self.output.n, 4, "output count")
    equal(self.output[1], value, "Val")
    equal(self.output[2], fresh, "Fsh")
    equal(self.output[3], sequence, "Seq")
    equal(self.output[4], heartbeat, "Hbt")
  end
  function r:count(prefix)
    local count = 0
    for _, value in ipairs(self.writes) do
      if value:sub(1, #prefix) == prefix then count = count + 1 end
    end
    return count
  end
  function r:begin(token)
    token = token or SESSION
    self:drain("ARGOS_RF_BEGIN " .. token .. "\n")
    equal(self.writes[#self.writes], "ARGOS_RF_READY " .. token .. "\n")
    self:expect(0, 0, 0, 0)
  end
  function r:set(sequence, value, time, token)
    self:step("ARGOS_RF_SET " .. (token or SESSION) .. " " .. sequence .. " " .. value .. "\n", time)
  end
  function r:blocked()
    local reads, writes, clocks = self.reads, #self.writes, self.clockReads
    self:step()
    self:expect(0, 0, 0, 0)
    equal(self.reads, reads)
    equal(#self.writes, writes)
    equal(self.clockReads, clocks)
  end
  return r
end

local function test(name, callback)
  testsRun = testsRun + 1
  local ok, message = pcall(callback)
  if ok then print("ok - " .. name) else
    testsFailed = testsFailed + 1
    print("not ok - " .. name .. ": " .. tostring(message))
  end
end

test("startup is neutral and advertises only the distinct RF protocol", function()
  local r = radio()
  equal(table.concat(r.script.output, ","), "Val,Fsh,Seq,Hbt")
  equal(#r.writes, 0)
  r:step()
  r:expect(0, 0, 0, 0)
  equal(r.writes[1], HELLO)
  assert(r.moduleReads[1] > 0 and r.moduleReads[2] > 0)
  for _, index in ipairs({2, 4, 6}) do equal(r.outputReads[index], 1) end
  r:step(nil, 49)
  equal(#r.writes, 1)
  r:step(nil, 50)
  equal(r.writes[2], HELLO)
end)

test("only the exact RF model and module/channel configuration is allowed", function()
  local mutations = {
    function(r) r.name = "ARGOS USB" end,
    function(r) r.name = "POCKET" end,
    function(r) r.name = "ARGOS RF " end,
    function(r) r.modules[1].Type = 0 end,
    function(r) r.modules[1].Type = "5" end,
    function(r) r.modules[1].Type = 6 end,
    function(r) r.modules[1].firstChannel = 1 end,
    function(r) r.modules[1].firstChannel = "0" end,
    function(r) r.modules[1].channelsCount = 8 end,
    function(r) r.modules[1].channelsCount = 17 end,
    function(r) r.modules[1].channelsCount = "16" end,
    function(r) r.modules[2].Type = 5 end,
    function(r) r.modules[2].Type = "0" end,
  }
  for _, mutate in ipairs(mutations) do local r = radio(); mutate(r); r:blocked() end
end)

test("missing and erroring model getters fail closed", function()
  local mutations = {
    function(r) r.infoMissing = true end,
    function(r) r.infoError = true end,
    function(r) r.moduleMissing[1] = true end,
    function(r) r.moduleMissing[2] = true end,
    function(r) r.moduleError[1] = true end,
    function(r) r.moduleError[2] = true end,
    function(r) r.modules[1].firstChannel = nil end,
    function(r) r.modules[1].channelsCount = nil end,
    function(r) r.modules[2].Type = nil end,
  }
  for _, mutate in ipairs(mutations) do local r = radio(); mutate(r); r:blocked() end
end)

test("each final output guard admits its inclusive low boundaries", function()
  for _, index in ipairs({2, 4, 6}) do
    for _, value in ipairs({-1100, -1024, -900}) do
      local r = radio()
      r.outputs[index] = value
      r:begin()
      r:set(1, 128)
      r:expect(128, 1024, 1, 1024)
    end
  end
end)

test("out-of-range, missing, nonnumeric and nonfinite outputs fail closed", function()
  local invalid = table.pack(-1101, -899, 0, 1024, nil, "-1024", false, {}, 0/0,
    math.huge, -math.huge)
  for _, index in ipairs({2, 4, 6}) do
    for item = 1, invalid.n do
      local r = radio()
      r.outputs[index] = invalid[item]
      r:blocked()
    end
  end
end)

test("unavailable or erroring final-output API fails without time or serial access", function()
  local missing = radio()
  missing.missing.getOutputValue = true
  missing:blocked()
  for _, index in ipairs({2, 4, 6}) do
    local r = radio()
    r.outputError[index] = true
    r:blocked()
  end
end)

test("any low-output guard dropping invalidates an active session", function()
  for _, index in ipairs({2, 4, 6}) do
    local r = radio()
    r:begin()
    r:set(1, 128)
    r:expect(128, 1024, 1, 1024)
    r.outputs[index] = 0
    r:blocked()
    r.outputs[index] = -1024
    r:set(2, -128)
    r:expect(0, 0, 0, 0)
    equal(r:count("ARGOS_RF_ACK"), 1)
    r:begin("1234abcd")
    r:set(1, -128, 1, "1234abcd")
    r:expect(-128, 1024, 1, 1024)
  end
end)

test("fragmented input requires both a session and a complete newline", function()
  local r = radio()
  r:set(1, 128)
  r:expect(0, 0, 0, 0)
  r:step("ARGOS_RF_BEGIN abc0")
  r:step("12ef\nARGOS_RF_SET abc012ef 1 ")
  r:step("128")
  r:expect(0, 0, 0, 0)
  r:step("\n")
  r:expect(128, 1024, 1, 1024)
  equal(r.writes[#r.writes], "ARGOS_RF_ACK abc012ef 1 128\n")
end)

test("the finite bench values preserve exact ACKs and heartbeat transitions", function()
  local r = radio()
  r:begin()
  for sequence, value in ipairs({0, 128, 0, -128, 0}) do
    r:set(sequence, value, sequence)
    r:expect(value, 1024, sequence, sequence % 2 == 1 and 1024 or -1024)
    equal(r.writes[#r.writes], "ARGOS_RF_ACK " .. SESSION .. " " .. sequence .. " " .. value .. "\n")
  end
end)

test("sequence gaps toggle per message rather than sequence parity", function()
  local r = radio()
  r:begin()
  r:set(2, 128)
  r:expect(128, 1024, 2, 1024)
  r:set(4, 128)
  r:expect(128, 1024, 4, -1024)
  r:set(119, 0)
  r:expect(0, 1024, 119, 1024)
end)

test("old MIX and display protocols cannot begin or update the RF session", function()
  local r = radio()
  r:drain("ARGOS_MIX_BEGIN " .. SESSION .. "\nARGOS_MIX_SET " .. SESSION
    .. " 1 128\nARGOS_USB_PING 1\n")
  r:expect(0, 0, 0, 0)
  equal(#r.writes, 1)
  r:begin()
  r:drain("ARGOS_MIX_SET " .. SESSION .. " 1 -128\n")
  r:expect(0, 0, 0, 0)
  equal(r:count("ARGOS_RF_ACK"), 0)
end)

test("invalid amplitudes and noncanonical numeric strings never renew heartbeat", function()
  local r = radio()
  r:begin()
  r:set(1, 128, 0)
  for _, value in ipairs({"256", "-256", "129", "-129", "1024", "1", "-1",
      "00", "-0", "+128", "128.0", "1.28e2", "NaN", "99999999999999999999"}) do
    r:set(2, value, 20)
    r:expect(128, 1024, 1, 1024)
  end
  equal(r:count("ARGOS_RF_ACK"), 1)
  r:step(nil, 30)
  r:expect(0, 0, 1, 0)
end)

test("replay, out-of-order, invalid sequence and wrong-session traffic do not extend TTL", function()
  local r = radio()
  r:begin()
  r:set(3, 128, 0)
  for _, sequence in ipairs({3, 2, 0, -1, 121, "04", "+4", "4.0", "4e0"}) do
    r:set(sequence, -128, 20)
    r:expect(128, 1024, 3, 1024)
  end
  r:set(4, -128, 20, "1234abcd")
  r:step("noise\n", 29)
  r:expect(128, 1024, 3, 1024)
  r:step(nil, 30)
  r:expect(0, 0, 3, 0)
  equal(r:count("ARGOS_RF_ACK"), 1)
end)

test("120 commands is a hard per-session sequence limit", function()
  local r = radio()
  r:begin()
  for sequence = 1, 120 do r:set(sequence, 128, sequence) end
  r:expect(128, 1024, 120, -1024)
  equal(r:count("ARGOS_RF_ACK"), 120)
  r:set(121, -128, 149)
  r:expect(128, 1024, 120, -1024)
  r:step(nil, 150)
  r:expect(0, 0, 120, 0)
  equal(r:count("ARGOS_RF_ACK"), 120)
end)

test("expiry reports idle once and a later accepted sequence starts heartbeat positive", function()
  local r = radio()
  r:begin()
  r:set(1, 128, 100)
  r:set(2, -128, 101)
  r:step(nil, 130)
  r:expect(-128, 1024, 2, -1024)
  r:step(nil, 131)
  r:expect(0, 0, 2, 0)
  equal(r.writes[#r.writes], "ARGOS_RF_IDLE " .. SESSION .. " 2\n")
  r:step(nil, 200)
  equal(r:count("ARGOS_RF_IDLE"), 1)
  r:set(3, 0, 201)
  r:expect(0, 1024, 3, 1024)
end)

test("only an eight-character lower-case hex BEGIN resets the session", function()
  local r = radio()
  r:begin()
  r:set(1, 128)
  for _, token in ipairs({"ABC012EF", "abc012e", "abc012ef0", "abc012eg", "abc012ef ", "abc012ef\r"}) do
    r:drain("ARGOS_RF_BEGIN " .. token .. "\n")
    r:expect(128, 1024, 1, 1024)
  end
  equal(r:count("ARGOS_RF_READY"), 1)
  r:begin("1234abcd")
  r:set(2, -128)
  r:expect(0, 0, 0, 0)
  r:set(1, -128, 1, "1234abcd")
  r:expect(-128, 1024, 1, 1024)
  r:begin("1234abcd")
end)

test("oversized input is discarded through its delimiter before valid traffic resumes", function()
  local r = radio()
  r:begin()
  r:drain(string.rep("x", 4096))
  r:drain("ARGOS_RF_SET " .. SESSION .. " 1 128\n")
  r:expect(0, 0, 0, 0)
  r:drain("\0ARGOS_RF_SET " .. SESSION .. " 1 128\n")
  r:expect(0, 0, 0, 0)
  r:set(1, -128)
  r:expect(-128, 1024, 1, 1024)
end)

test("guard failure discards a partial frame and requires a fresh session", function()
  local r = radio()
  r:begin()
  r:step("ARGOS_RF_SET " .. SESSION .. " 1 ")
  r.modules[1].channelsCount = 8
  r:blocked()
  r.modules[1].channelsCount = 16
  r:step("128\n")
  r:expect(0, 0, 0, 0)
  r:set(1, 128)
  r:expect(0, 0, 0, 0)
  r:begin()
  r:set(1, -128)
  r:expect(-128, 1024, 1, 1024)
end)

test("missing serial APIs drop active state without further I/O", function()
  for _, name in ipairs({"serialRead", "serialWrite"}) do
    local r = radio()
    r:begin()
    r:set(1, 128)
    r.missing[name] = true
    r:blocked()
  end
end)

test("expiry survives clock wrap and script reload retains no session", function()
  local r = radio()
  r.now = 4294967286
  r:begin()
  r:set(1, 128)
  r:step(nil, 19)
  r:expect(128, 1024, 1, 1024)
  r:step(nil, 20)
  r:expect(0, 0, 1, 0)
  local fresh = radio()
  fresh:set(2, 128)
  fresh:expect(0, 0, 0, 0)
end)

assert(testsFailed == 0, testsFailed .. " of " .. testsRun .. " RF yaw tests failed")
print("Passed " .. testsRun .. " RF yaw tests (mocked scheduled callbacks; no aircraft).")
