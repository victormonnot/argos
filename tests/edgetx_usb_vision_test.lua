-- Run from the repository root with Lua 5.2 or later:
--   lua tests/edgetx_usb_vision_test.lua
-- This checks scheduled callbacks in a restricted mock, not radio hardware or
-- the behavior of an EdgeTX mixer after Lua stops executing.

local scriptPath = arg[1] or "scripts/edgetx/ArgVis.lua"
local HELLO = "ARGOS_USB_VISION_BENCH_V1\n"
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
    name = options.name or "ARGOS VISION", moduleTypes = {0, 0},
    modelReads = 0, moduleReads = {0, 0}, infoMissing = false,
    moduleMissing = {}, infoError = false, moduleError = {},
    missingRead = false, missingWrite = false, readError = false, writeError = false,
    badRead = false,
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
      if state.readError then error("mock serial read error") end
      if state.badRead then return nil end
      assert(type(count) == "number" and count >= 1 and count <= 64,
        "serial read must request a bounded number of bytes")
      local value = state.input:sub(1, count)
      state.input = state.input:sub(count + 1)
      return value
    end,
    serialWrite = function(value)
      if state.writeError then error("mock serial write error") end
      assert(type(value) == "string" and #value <= 64,
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
    assert(self.output[1] >= -128 and self.output[1] <= 128, "value outside bench bound")
    assert(self.output[2] == 0 or self.output[2] == 1024, "invalid freshness")
    assert(self.output[3] >= 0 and self.output[3] <= 300, "invalid sequence output")
    assert(self.output[4] == -1024 or self.output[4] == 0 or self.output[4] == 1024,
      "invalid heartbeat output")
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

  function state:expect(value, fresh, sequence, heartbeat)
    equal(self.output.n, 4, "mixer must return exactly four values")
    equal(self.output[1], value, "value output")
    equal(self.output[2], fresh, "freshness output")
    equal(self.output[3], sequence, "sequence output")
    if heartbeat ~= nil then
      equal(self.output[4], heartbeat, "heartbeat output")
    elseif fresh == 0 then
      equal(self.output[4], 0, "inactive heartbeat output")
    else
      assert(self.output[4] == -1024 or self.output[4] == 1024,
        "active heartbeat must have a full-scale polarity")
    end
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
    self:drain("ARGOS_VISION_BEGIN " .. session .. "\n")
    equal(self.writes[#self.writes], "ARGOS_VISION_READY " .. session .. "\n")
    self:expect(0, 0, 0)
  end

  function state:set(sequence, value, time, session, ttl)
    session = session or SESSION
    self:step("ARGOS_VISION_SET " .. session .. " " .. sequence .. " " .. value .. " " .. (ttl or 20) .. "\n", time)
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

test("cold radio greets every 50 ticks with four neutral named outputs", function()
  local r = radio()
  equal(table.concat(r.script.output, ","), "Val,Fsh,Seq,Hbt")
  r:step()
  r:expect(0, 0, 0, 0)
  equal(r.writes[1], HELLO)
  r:step(nil, 49)
  equal(#r.writes, 1)
  r:step(nil, 50)
  equal(r.writes[2], HELLO)
  equal(r.modelReads, 3)
  equal(r.moduleReads[1], 3)
  equal(r.moduleReads[2], 3)
end)

test("only the exact ARGOS VISION model with both RF modules OFF is permitted", function()
  for _, name in ipairs({"ARGOS USB", "ARGOS RF", "argos vision", "ARGOS VISION "}) do
    local r = radio({name = name})
    r:step("ARGOS_VISION_BEGIN " .. SESSION .. "\n")
    r:expect(0, 0, 0, 0)
    equal(r.reads, 0)
    equal(#r.writes, 0)
  end
  for module = 1, 2 do
    for _, kind in ipairs({1, 5, -1, "0", false}) do
      local r = radio()
      r.moduleTypes[module] = kind
      r:step()
      r:expect(0, 0, 0, 0)
      equal(r.reads, 0)
      equal(#r.writes, 0)
    end
  end
end)

test("missing model/module information and getter errors fail closed", function()
  local failures = {
    function(r) r.infoMissing = true end,
    function(r) r.infoError = true end,
    function(r) r.moduleMissing[1] = true end,
    function(r) r.moduleMissing[2] = true end,
    function(r) r.moduleError[1] = true end,
    function(r) r.moduleError[2] = true end,
    function(r) r.moduleTypes[1] = nil end,
    function(r) r.moduleTypes[2] = nil end,
  }
  for _, fail in ipairs(failures) do
    local r = radio()
    fail(r)
    r:step()
    r:expect(0, 0, 0, 0)
    equal(r.reads, 0)
    equal(#r.writes, 0)
  end
end)

test("every continuous integer in the bounded range can be acknowledged", function()
  local r = radio()
  r:begin()
  for value = -128, 128 do
    local sequence = value + 129
    r:set(sequence, value, sequence)
    r:expect(value, 1024, sequence)
    equal(r.writes[#r.writes], "ARGOS_VISION_ACK " .. SESSION .. " "
      .. sequence .. " " .. value .. " 20\n")
  end
  equal(r:countWrites("ARGOS_VISION_ACK"), 257)
end)

test("session handshake is required and partial command input stays neutral", function()
  local r = radio()
  r:set(1, 128)
  r:expect(0, 0, 0, 0)
  r:step("ARGOS_VISION_BEGIN abc0")
  r:step("12ef\nARGOS_VISION_SET abc012ef 1 ")
  r:step("128 20")
  r:expect(0, 0, 0, 0)
  r:step("\n")
  r:expect(128, 1024, 1, 1024)
end)

test("only strictly increasing canonical sequences 1 through 300 are admitted", function()
  local r = radio()
  r:begin()
  r:set(3, 128)
  for _, seq in ipairs({0, 1, 3, 301, -1, "04", "+4", "4.0", "4e0",
      "999999999999999999999999", "NaN", "inf"}) do
    r:set(seq, -128, 1)
    r:expect(128, 1024, 3, 1024)
  end
  r:set(300, 35, 2)
  r:expect(35, 1024, 300, -1024)
  r:set(301, 0, 3)
  r:expect(35, 1024, 300, -1024)
  equal(r:countWrites("ARGOS_VISION_ACK"), 2)
end)

test("noncanonical, floating point and out-of-range values cannot renew output", function()
  local r = radio()
  r:begin()
  r:set(1, 128)
  for _, value in ipairs({"129", "-129", "1024", "-1024", "01", "00", "-0",
      "+1", "1.0", "1e0", "1.5", "NaN", "nan", "inf", "-inf",
      "9999999999999999999999"}) do
    r:set(2, value, 10)
    r:expect(128, 1024, 1, 1024)
  end
  r:set(2, -128, 10, "deadbeef")
  r:expect(128, 1024, 1, 1024)
  r:step(nil, 20)
  r:expect(0, 0, 1, 0)
  equal(r:countWrites("ARGOS_VISION_ACK"), 1)
end)

test("TTL accepts only canonical integers from 1 through 20 ticks", function()
  for ttl = 1, 20 do
    local r = radio()
    r:begin()
    r:set(1, 12, 0, nil, ttl)
    r:step(nil, ttl - 1)
    r:expect(12, 1024, 1, 1024)
    r:step(nil, ttl)
    r:expect(0, 0, 1, 0)
  end
  local r = radio()
  r:begin()
  r:set(1, 40)
  for _, ttl in ipairs({0, 21, -1, "01", "+1", "1.0", "1e0", "NaN",
      "inf", "99999999999999999999"}) do
    r:set(2, -40, 5, nil, ttl)
    r:expect(40, 1024, 1, 1024)
  end
  equal(r:countWrites("ARGOS_VISION_ACK"), 1)
end)

test("expiry runs before queued late SET input and latches the old session", function()
  local r = radio()
  r:begin()
  r:set(1, 128, 0, nil, 10)
  r:set(2, -128, 10)
  r:expect(0, 0, 1, 0)
  equal(r.writes[#r.writes], "ARGOS_VISION_IDLE " .. SESSION .. " 1\n")
  r:step("ARGOS_VISION_BEGIN " .. SESSION .. "\n", 11)
  r:set(2, 128, 12)
  r:expect(0, 0, 1, 0)
  equal(r:countWrites("ARGOS_VISION_ACK"), 1)
  equal(r:countWrites("ARGOS_VISION_READY"), 1)
  r:step(nil, 30)
  equal(r:countWrites("ARGOS_VISION_IDLE"), 1)
  r:begin("1234abcd")
  r:set(1, -24, 31, "1234abcd")
  r:expect(-24, 1024, 1, 1024)
end)

test("a repeated active BEGIN cannot reset sequence, heartbeat or TTL", function()
  local r = radio()
  r:begin()
  r:set(1, 128, 0)
  r:step("ARGOS_VISION_BEGIN " .. SESSION .. "\n", 19)
  r:expect(128, 1024, 1, 1024)
  equal(r:countWrites("ARGOS_VISION_READY"), 1)
  r:set(1, -128, 19)
  r:expect(128, 1024, 1, 1024)
  r:step(nil, 20)
  r:expect(0, 0, 1, 0)
end)

test("session expires at 30 seconds even with fresh values or repeated BEGIN", function()
  local r = radio()
  r:begin()
  -- No SET means no command TTL yet, but the initial session deadline remains.
  r:step("ARGOS_VISION_BEGIN " .. SESSION .. "\n", 2990)
  r:set(1, 128, 2991)
  r:set(2, 127, 2999)
  r:expect(127, 1024, 2, -1024)
  r:set(3, -128, 3000)
  r:expect(0, 0, 2, 0)
  equal(r:countWrites("ARGOS_VISION_IDLE"), 1)
  equal(r:countWrites("ARGOS_VISION_READY"), 1)
  r:step("ARGOS_VISION_BEGIN " .. SESSION .. "\n", 3001)
  r:set(3, -128, 3002)
  r:expect(0, 0, 2, 0)
  local unused = radio()
  unused:begin()
  unused:step(nil, 3000)
  equal(unused:countWrites("ARGOS_VISION_IDLE"), 1)
  unused:set(1, 12, 3001)
  unused:expect(0, 0, 0, 0)
end)

test("a distinct BEGIN clears output and rejects the previous session", function()
  local r = radio()
  r:begin()
  r:set(1, 128)
  r:begin("1234abcd")
  r:set(2, 100)
  r:expect(0, 0, 0, 0)
  r:set(1, -128, 1, "1234abcd")
  r:expect(-128, 1024, 1, 1024)
end)

test("invalid BEGIN syntax cannot disturb an active session", function()
  local r = radio()
  r:begin()
  r:set(1, 20)
  for _, token in ipairs({"ABC012EF", "abc012e", "abc012ef0", "abc012eg",
      "abc0 2ef", "abc012ef ", "abc012ef\r"}) do
    r:drain("ARGOS_VISION_BEGIN " .. token .. "\n")
    r:expect(20, 1024, 1, 1024)
  end
  equal(r:countWrites("ARGOS_VISION_READY"), 1)
end)

test("heartbeat changes only with accepted SET including same value and sequence gaps", function()
  local r = radio()
  r:begin()
  r:set(2, 128, 0)
  r:expect(128, 1024, 2, 1024)
  r:set(4, 128, 1)
  r:expect(128, 1024, 4, -1024)
  r:set(7, 0, 2)
  r:expect(0, 1024, 7, 1024)
  r:step(nil, 3)
  r:set(7, 12, 4)
  r:set(6, 12, 5)
  r:step("garbage\n", 6)
  r:expect(0, 1024, 7, 1024)
  r:step(nil, 22)
  r:expect(0, 0, 7, 0)
end)

test("every runtime guard failure clears active output and partial input", function()
  local failures = {
    {function(r) r.name = "ARGOS RF" end, function(r) r.name = "ARGOS VISION" end},
    {function(r) r.moduleTypes[1] = 5 end, function(r) r.moduleTypes[1] = 0 end},
    {function(r) r.moduleTypes[2] = 5 end, function(r) r.moduleTypes[2] = 0 end},
    {function(r) r.infoError = true end, function(r) r.infoError = false end},
    {function(r) r.moduleError[1] = true end, function(r) r.moduleError[1] = false end},
    {function(r) r.missingRead = true end, function(r) r.missingRead = false end},
    {function(r) r.missingWrite = true end, function(r) r.missingWrite = false end},
  }
  for _, pair in ipairs(failures) do
    local r = radio()
    r:begin()
    r:set(1, 128)
    r:step("ARGOS_VISION_SET " .. SESSION .. " 2 ")
    local reads, writes = r.reads, #r.writes
    pair[1](r)
    r:step()
    r:expect(0, 0, 0, 0)
    equal(r.reads, reads)
    equal(#r.writes, writes)
    pair[2](r)
    r:step("128 20\n")
    r:set(2, 128)
    r:expect(0, 0, 0, 0)
    r:begin("1234abcd")
    r:set(1, -128, 1, "1234abcd")
    r:expect(-128, 1024, 1, 1024)
  end
end)

test("serial read errors, invalid reads and write errors clear active output", function()
  for _, field in ipairs({"readError", "badRead", "writeError"}) do
    local r = radio()
    r:begin()
    r:set(1, 128)
    r[field] = true
    r:set(2, -128, 1)
    r:expect(0, 0, 0, 0)
  end
end)

test("oversized input cannot smuggle an accepted suffix or starve the deadline", function()
  local r = radio()
  r:begin()
  r:drain(string.rep("x", 4096))
  r:drain("ARGOS_VISION_SET " .. SESSION .. " 1 128 20\n")
  r:expect(0, 0, 0, 0)
  r:drain("noise\n\0ARGOS_VISION_SET " .. SESSION .. " 1 128 20\n")
  r:expect(0, 0, 0, 0)
  r:set(1, 128)
  r:step(string.rep("x", 4096), 20)
  r:expect(0, 0, 1, 0)
  equal(r:countWrites("ARGOS_VISION_ACK"), 1)
end)

test("command TTL, session deadline and greeting tolerate clock wrap", function()
  local r = radio({now = 4294967286})
  r:begin()
  r:set(1, 128)
  r:step(nil, 9)
  r:expect(128, 1024, 1, 1024)
  r:step(nil, 10)
  r:expect(0, 0, 1, 0)
  r:step(nil, 40)
  equal(r:countWrites(HELLO), 2)
  local deadline = radio({now = 4294967000})
  deadline:begin()
  deadline:set(1, 128, 2699)
  deadline:expect(128, 1024, 1, 1024)
  deadline:set(2, -128, 2704)
  deadline:expect(0, 0, 1, 0)
end)

test("script reload starts neutral and needs a new handshake", function()
  local r = radio()
  r:begin()
  r:set(1, 128)
  local reloaded = radio()
  reloaded:set(2, 128)
  reloaded:expect(0, 0, 0, 0)
  reloaded:begin("1234abcd")
  reloaded:set(1, -128, 0, "1234abcd")
  reloaded:expect(-128, 1024, 1, 1024)
end)

assert(testsFailed == 0, testsFailed .. " of " .. testsRun .. " EdgeTX vision tests failed")
print("Passed " .. testsRun .. " EdgeTX vision tests (scheduled callbacks; RF-off mock only).")
