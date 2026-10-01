-- Thin test-only adapter: Python supplies clock/inputs/bytes to the actual Lua
-- script through the shared fixture. No serial device or EdgeTX model is opened.
local radio = dofile("tests/edgetx_yaw_stream_fixture.lua")
local r = radio()
local written = 0

local function unhex(text)
  if text == "-" then return "" end
  assert(#text % 2 == 0 and not text:find("[^0-9a-f]"), "bad test packet")
  return (text:gsub("..", function(pair) return string.char(tonumber(pair, 16)) end))
end

local function hex(text)
  if text == "" then return "-" end
  return (text:gsub(".", function(c) return string.format("%02x", string.byte(c)) end))
end

for line in io.lines() do
  local tick, sc, rud, ele, ail, thr, takeover, packet = line:match(
    "^(%d+) ([%-]?%d+) ([%-]?%d+) ([%-]?%d+) ([%-]?%d+) ([%-]?%d+) ([01]) ([0-9a-f%-]+)$")
  assert(tick, "bad test step")
  r.sc, r.rud, r.native = tonumber(sc), tonumber(rud), takeover == "1"
  r.ele, r.ail, r.thr = tonumber(ele), tonumber(ail), tonumber(thr)
  local value, fresh, sequence, heartbeat = r:step(unhex(packet), tonumber(tick))
  local replies = {}
  for i = written + 1, #r.writes do replies[#replies + 1] = r.writes[i] end
  written = #r.writes
  io.write(string.format("%d %d %d %d %s\n", value, fresh, sequence, heartbeat,
    hex(table.concat(replies))))
  io.flush()
end
