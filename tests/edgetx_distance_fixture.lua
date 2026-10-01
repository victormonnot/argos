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
    now = options.now or 0, sc = options.sc or 0, rud = 0, ele = 0, ail = 0, thr = -1024, sb = -1024, native = false,
    name = options.name or "ARGOS DST", flip = -1024,
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
      if source == "rud" then return r.rud end
      if source == "ele" then return r.ele end
      if source == "ail" then
        if r.pilotReadError then error("optional pilot read failed") end
        return r.ail
      end
      if source == "thr" then return r.thr end
      equal(source, "sb", "only raw yaw/pitch/SC/SB may be read")
      return r.sb
    end,
    getOutputValue = function(index)
      equal(index, 6, "only crash-flip output may be checked")
      return r.flip
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
      equal(count, 96, "bounded read")
      r.reads = r.reads + 1
      if r.readError then error("read failed") end
      if r.badRead then return nil end
      local data = r.input:sub(1, count)
      r.input = r.input:sub(count + 1)
      return data
    end,
    serialWrite = function(line)
      if r.writeError then error("write failed") end
      if r.pilotWriteError and line:sub(1, 4) == "AP1 " then error("optional pilot write failed") end
      assert(type(line) == "string" and #line <= 96, "bounded wire output")
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
  r.script = checkedCall(assert(loadfile(options.script or "scripts/edgetx/ArgDst.lua",
    "t", environment)))

  function r:step(input, time)
    self.input = self.input .. (input or "")
    if time ~= nil then self.now = time end
    local before = self.reads
    self.output = table.pack(checkedCall(self.script.run))
    self.lastReads = self.reads - before
    equal(self.output.n, 6, "six native outputs")
    assert(self.lastReads <= 4, "more than 384 bytes read per callback")
    assert(self.output[1] >= -205 and self.output[1] <= 205, "yaw bound")
    assert(self.output[2] == 0 or self.output[2] == 1024, "freshness flag")
    assert(self.output[3] == 0 or self.output[3] == 1024, "sequence marker")
    assert(self.output[4] == -1024 or self.output[4] == 0 or self.output[4] == 1024,
      "heartbeat flag")
    assert(self.output[5] >= -51 and self.output[5] <= 51, "pitch bound")
    assert(self.output[6] == 0 or self.output[6] == 1024, "pitch freshness")
    return table.unpack(self.output, 1, self.output.n)
  end

  function r:expect(value, fresh, heartbeat)
    equal(self.output[1], value, "yaw")
    equal(self.output[2], fresh, "freshness")
    if heartbeat ~= nil then equal(self.output[4], heartbeat, "heartbeat") end
  end

  function r:latestStatus()
    for i = #self.writes, 1, -1 do
      local session, generation, ticket, sequence, state, cause, mode, yawPhase, pitchPhase = self.writes[i]:match(
        "^DY3 ([0-9a-f]+) (%d+) (%d+) (%d+) ([MTAF]) ([SMWATEPLIGCO]) ([NYD]) ([NAMR]) ([NAMR])\n$")
      if session then return {session = session, generation = tonumber(generation),
        ticket = tonumber(ticket), sequence = tonumber(sequence), state = state, cause = cause, mode = mode, yawPhase = yawPhase, pitchPhase = pitchPhase} end
    end
    error("no status emitted")
  end

  function r:begin(session)
    self:step("#\nDB3 " .. (session or "abc012ef") .. "\n")
    return self:latestStatus()
  end

  function r:arm(mode)
    self.sc = 0
    self:step()
    self.sc = mode == "D" and 1024 or -1024
    self:step()
    equal(self:latestStatus().state, "T", "SC middle/up enables waiting state")
    return self:latestStatus()
  end

  function r:command(sequence, value, valid, status, pitch, pitchValid)
    local s = status or self:latestStatus()
    if valid == nil then valid = true end
    return string.format("DS3 %s %d %d %d %d %d %d %d\n", s.session,
      s.generation, s.ticket, sequence, valid and 1 or 0, value, pitchValid and 1 or 0, pitch or 0)
  end

  function r:set(sequence, value, valid, time, status, pitch, pitchValid)
    self:step(self:command(sequence, value, valid, status, pitch, pitchValid), time)
  end

  return r
end

return radio
