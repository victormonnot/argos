-- Run from the repository root with Lua 5.2 or later:
--   lua tests/edgetx_usb_display_test.lua
-- This mocks the display and serial APIs; it does not validate radio hardware.

local scriptPath = arg[1] or "scripts/edgetx/ArgosUSB.lua"
local READY = "ARGOS_USB_DISPLAY_V1\n"
local EXIT_EVENT = 100
local testsRun = 0

local function equal(actual, expected, message)
  assert(actual == expected, (message or "unexpected result")
    .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
end

local function checkedCall(callback, ...)
  local values = {...}
  debug.sethook(function()
    error("callback exceeded its instruction budget", 0)
  end, "", 100000)
  local ok, result = pcall(callback, table.unpack(values))
  debug.sethook()
  assert(ok, result)
  return result
end

local function radio(options)
  options = options or {}
  local state = {
    now = 0, input = "", writes = {}, screen = {}, reads = 0,
    clockReads = 0, lcdCalls = 0,
  }
  local environment = {
    string = string, math = math, table = table,
    tonumber = tonumber, tostring = tostring, type = type,
    pairs = pairs, ipairs = ipairs,
    SMLSIZE = 1, MIDSIZE = 2, DBLSIZE = 4, INVERS = 8, BLINK = 16,
    LEFT = 0, RIGHT = 32, CENTER = 64,
    EVT_VIRTUAL_EXIT = EXIT_EVENT,
    getTime = function()
      state.clockReads = state.clockReads + 1
      return state.now
    end,
    serialRead = function(count)
      state.reads = state.reads + 1
      assert(type(count) == "number" and count >= 1 and count <= 256,
        "serial read must request a bounded number of bytes")
      local value = state.input:sub(1, count)
      state.input = state.input:sub(count + 1)
      return value
    end,
    serialWrite = function(value)
      assert(type(value) == "string", "serial writes must be strings")
      local sequence = value:match("^ARGOS_USB_ACK (%d+)\n$")
      assert(value == READY or (sequence and tonumber(sequence) >= 1
        and tonumber(sequence) <= 120), "unexpected serial output: " .. value)
      state.writes[#state.writes + 1] = value
    end,
    lcd = {
      clear = function()
        state.lcdCalls = state.lcdCalls + 1
        state.screen = {}
      end,
      drawText = function(x, y, value)
        state.lcdCalls = state.lcdCalls + 1
        state.screen[#state.screen + 1] = tostring(value)
      end,
      drawNumber = function(x, y, value)
        state.lcdCalls = state.lcdCalls + 1
        state.screen[#state.screen + 1] = tostring(value)
      end,
    },
  }
  setmetatable(environment, {
    __index = function(_, key)
      if key == "serialRead" or key == "serialWrite" then return nil end
      error("script accessed an undeclared capability: " .. tostring(key), 0)
    end,
    __newindex = function(_, key)
      error("script wrote a global: " .. tostring(key), 0)
    end,
  })
  if options.missingRead then environment.serialRead = nil end
  if options.missingWrite then environment.serialWrite = nil end
  local chunk = assert(loadfile(scriptPath, "t", environment))
  state.script = checkedCall(chunk)
  assert(type(state.script) == "table" and type(state.script.run) == "function")
  if state.script.init then checkedCall(state.script.init) end

  function state:step(input, time, event)
    self.input = self.input .. (input or "")
    if time then self.now = time end
    return checkedCall(self.script.run, event or 0)
  end

  function state:text()
    return table.concat(self.screen, " ")
  end

  function state:shows(value)
    assert(self:text():find(value, 1, true), "missing screen text: " .. value
      .. " (screen: " .. self:text() .. ")")
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

  function state:ackCount()
    local count = 0
    for _, value in ipairs(self.writes) do
      if value ~= READY then count = count + 1 end
    end
    return count
  end

  return state
end

local function test(name, callback)
  callback()
  testsRun = testsRun + 1
  print("ok - " .. name)
end

test("loading does not start serial or display activity", function()
  local r = radio()
  equal(r.reads, 0)
  equal(#r.writes, 0)
  equal(r.lcdCalls, 0)
end)

test("waiting announces the display protocol without acknowledging input", function()
  local r = radio()
  equal(r:step(), 0)
  equal(r.writes[1], READY)
  equal(r:ackCount(), 0)
  r:shows("WAITING FOR PC")
  r:shows("Received: 0")
  r:shows("DISPLAY ONLY")
  r:step(nil, 49)
  equal(#r.writes, 1, "greeting should not be flooded")
  r:step(nil, 50)
  equal(r.writes[2], READY, "late-starting host must receive another greeting")
end)

test("fragmented ping is accepted only after its newline", function()
  local r = radio()
  r:step("ARGOS_USB_PI")
  r:step("NG 17")
  equal(r:ackCount(), 0)
  r:shows("Received: 0")
  r:step("\n")
  equal(r:ackCount(), 1)
  equal(r.writes[#r.writes], "ARGOS_USB_ACK 17\n")
  r:shows("RECEIVING")
  r:shows("Received: 1")
  r:shows("Last: 17")
end)

test("coalesced messages preserve order and count both allowed boundaries", function()
  local r = radio()
  r:drain("ARGOS_USB_PING 1\nARGOS_USB_PING 37\nARGOS_USB_PING 120\n")
  equal(r:ackCount(), 3)
  equal(r.writes[2], "ARGOS_USB_ACK 1\n")
  equal(r.writes[3], "ARGOS_USB_ACK 37\n")
  equal(r.writes[4], "ARGOS_USB_ACK 120\n")
  r:shows("Received: 3")
  r:shows("Last: 120")
end)

test("malformed noise never earns an acknowledgement and valid traffic recovers", function()
  local r = radio()
  r:drain(table.concat({
    "", "arbitrary noise", "ARGOS_USB_PING 0", "ARGOS_USB_PING 121",
    "ARGOS_USB_PING -1", "ARGOS_USB_PING +1", "ARGOS_USB_PING 01",
    "ARGOS_USB_PING 1.0", "ARGOS_USB_PING 1e1", " ARGOS_USB_PING 1",
    "ARGOS_USB_PING 1 ", "ARGOS_USB_PING 1\r", "ARGOS_USB_PING 1\0",
    "ARGOS_USB_PING NaN", "ARGOS_USB_ACK 2", "ARGOS_USB_DISPLAY_V1",
  }, "\n") .. "\n")
  equal(r:ackCount(), 0)
  r:shows("Received: 0")
  r:step("ARGOS_USB_PING 2\n")
  equal(r:ackCount(), 1)
  equal(r.writes[#r.writes], "ARGOS_USB_ACK 2\n")
end)

test("oversized unterminated input cannot smuggle a valid suffix", function()
  local r = radio()
  r:drain(string.rep("x", 4096))
  equal(r:ackCount(), 0)
  r:drain("ARGOS_USB_PING 8\n")
  equal(r:ackCount(), 0, "oversized line must be discarded through its newline")
  r:drain("ARGOS_USB_PING 9\n")
  equal(r:ackCount(), 1)
  r:shows("Received: 1")
  r:shows("Last: 9")
end)

test("status goes stale after 1.5 seconds without discarding the last result", function()
  local r = radio()
  r:step("ARGOS_USB_PING 3\n", 100)
  r:step(nil, 249)
  r:shows("RECEIVING")
  r:step(nil, 250)
  r:shows("NO RECENT MESSAGE")
  r:shows("Received: 1")
  r:shows("Last: 3")
end)

test("invalid traffic does not refresh freshness and later valid traffic does", function()
  local r = radio()
  r:step("ARGOS_USB_PING 4\n", 0)
  r:step("noise\nARGOS_USB_PING 121\n", 149)
  r:step("ARGOS_USB_PING 0\n", 150)
  r:shows("NO RECENT MESSAGE")
  equal(r:ackCount(), 1)
  r:step("ARGOS_USB_PING 5\n", 151)
  r:shows("RECEIVING")
  r:shows("Received: 2")
  r:shows("Last: 5")
end)

test("freshness survives the radio clock wrapping", function()
  local r = radio()
  r:step("ARGOS_USB_PING 6\n", 4294967246)
  r:step(nil, 99)
  r:shows("RECEIVING")
  r:step(nil, 100)
  r:shows("NO RECENT MESSAGE")
end)

test("exit consumes no queued input and permanently stops activity", function()
  local r = radio()
  r:step("ARGOS_USB_PING 7\n")
  local reads, writes, clocks, drawings = r.reads, #r.writes, r.clockReads, r.lcdCalls
  equal(r:step("ARGOS_USB_PING 8\n", 1, EXIT_EVENT), 2)
  equal(r:step("ARGOS_USB_PING 9\n", 1000), 2)
  equal(r.reads, reads, "exit must stop serial reads")
  equal(#r.writes, writes, "exit must stop serial writes")
  equal(r.clockReads, clocks, "exit must stop clock access")
  equal(r.lcdCalls, drawings, "exit must stop rendering")
  equal(r.input, "ARGOS_USB_PING 8\nARGOS_USB_PING 9\n")
end)

test("missing serial-read API is a visible error without serial output", function()
  local r = radio({missingRead = true})
  equal(r:step(), 0)
  r:shows("Serial API missing")
  equal(#r.writes, 0)
end)

test("missing serial-write API is a visible error without input consumption", function()
  local r = radio({missingWrite = true})
  equal(r:step("ARGOS_USB_PING 1\n"), 0)
  r:shows("Serial API missing")
  equal(r.reads, 0)
end)

print("Passed " .. testsRun .. " EdgeTX display tests (mocked APIs; no hardware).")
