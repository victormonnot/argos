-- Restricted, read-only radio mock shared by Lua and cross-language tests.
-- No simulated callback scheduler: tests explicitly advance the radio clock.
local function equal(actual, expected, message)
  assert(actual == expected, (message or "unexpected result")
    .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
end

local function checkedCall(callback, ...)
  local arguments = table.pack(...)
  debug.sethook(function() error("callback instruction budget exceeded", 0) end,
    "", 100000)
  local values = table.pack(pcall(callback, table.unpack(arguments, 1, arguments.n)))
  debug.sethook()
  assert(values[1], values[2])
  return table.unpack(values, 2, values.n)
end

local function readonly(values, name)
  return setmetatable({}, {
    __index = values,
    __newindex = function(_, key)
      error("attempt to write " .. name .. "." .. tostring(key), 0)
    end,
  })
end

local function radio(options)
  options = options or {}
  local r = {
    now = options.now or 0, sc = options.sc or 0, rud = 0, native = false,
    name = options.name or "ARGOS FLY", flip = -1024,
    internal = {Type = 5, firstChannel = 0, channelsCount = 16}, external = {Type = 0},
    input = "", writes = {}, reads = 0, lastReads = 0, readError = false,
    writeError = false, badRead = false, missingRead = false, missingWrite = false,
  }
  local allowed = {
    math = math, string = string, tonumber = tonumber,
    tostring = tostring, type = type, pcall = pcall, ipairs = ipairs,
    getTime = function()
      if r.clockError then error("clock unavailable") end
      return r.now
    end,
    getValue = function(source)
      if source == "sc" then return r.sc end
      equal(source, "rud", "only raw yaw/SC sources may be accessed")
      return r.rud
    end,
    getOutputValue = function(index)
      equal(index, 6, "only crash-flip output may be checked")
      return r.flip
    end,
    getLogicalSwitchValue = function(index)
      equal(index, 9, "native L10 must be read")
      return r.native
    end,
    model = readonly({
      getInfo = function()
        if r.modelError then error("model unavailable") end
        if r.modelMissing then return nil end
        return readonly({name = r.name}, "model information")
      end,
      getModule = function(index)
        assert(index == 0 or index == 1, "unexpected RF module")
        local module = index == 0 and r.internal or r.external
        return module and readonly(module, "RF module") or nil
      end,
    }, "model"),
    serialRead = function(count)
      equal(count, 64, "bounded read")
      r.reads = r.reads + 1
      if r.readError then error("read failed") end
      if r.badRead then return nil end
      local data = r.input:sub(1, count)
      r.input = r.input:sub(count + 1)
      return data
    end,
    serialWrite = function(line)
      if r.writeError then error("write failed") end
      assert(type(line) == "string" and #line <= 64, "bounded wire output")
      r.writes[#r.writes + 1] = line
    end,
  }
  local environment = setmetatable({}, {
    __index = function(_, key)
      -- Pocket's runtime has no global table library; Lua table values work.
      if key == "table" then return nil end
      if (key == "serialRead" and r.missingRead)
          or (key == "serialWrite" and r.missingWrite) then return nil end
      assert(allowed[key] ~= nil, "undeclared capability: " .. tostring(key))
      return allowed[key]
    end,
    __newindex = function(_, key) error("global write: " .. tostring(key), 0) end,
  })
  r.script = checkedCall(assert(loadfile(options.script or "scripts/edgetx/ArgFly.lua",
    "t", environment)))

  function r:step(input, time)
    self.input = self.input .. (input or "")
    if time ~= nil then self.now = time end
    local before = self.reads
    self.output = table.pack(checkedCall(self.script.run))
    self.lastReads = self.reads - before
    equal(self.output.n, 4, "four native outputs")
    assert(self.lastReads <= 4, "more than 256 bytes read per callback")
    assert(self.output[1] >= -205 and self.output[1] <= 205, "yaw bound")
    assert(self.output[2] == 0 or self.output[2] == 1024, "freshness flag")
    assert(self.output[3] == 0 or self.output[3] == 1024, "sequence marker")
    assert(self.output[4] == -1024 or self.output[4] == 0 or self.output[4] == 1024,
      "heartbeat flag")
    return table.unpack(self.output, 1, self.output.n)
  end

  function r:expect(value, fresh, heartbeat)
    equal(self.output[1], value, "yaw")
    equal(self.output[2], fresh, "freshness")
    if heartbeat ~= nil then equal(self.output[4], heartbeat, "heartbeat") end
  end

  function r:latestStatus()
    for i = #self.writes, 1, -1 do
      local session, generation, ticket, sequence, state, cause = self.writes[i]:match(
        "^AY1 ([0-9a-f]+) (%d+) (%d+) (%d+) ([MTAF]) ([SMWATEPLIGCO])\n$")
      if session then return {session = session, generation = tonumber(generation),
        ticket = tonumber(ticket), sequence = tonumber(sequence), state = state, cause = cause} end
    end
    error("no status emitted")
  end

  function r:begin(session)
    self:step("#\nAB1 " .. (session or "abc012ef") .. "\n")
    return self:latestStatus()
  end

  function r:arm()
    self.sc = 0
    self:step()
    self.sc = -1024
    self:step()
    equal(self:latestStatus().state, "T", "SC middle/up enables waiting state")
    return self:latestStatus()
  end

  function r:command(sequence, value, valid, status)
    local s = status or self:latestStatus()
    if valid == nil then valid = true end
    return string.format("AS1 %s %d %d %d %d %d\n", s.session,
      s.generation, s.ticket, sequence, valid and 1 or 0, value)
  end

  function r:set(sequence, value, valid, time, status)
    self:step(self:command(sequence, value, valid, status), time)
  end

  return r
end

return radio
