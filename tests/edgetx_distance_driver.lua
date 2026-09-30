-- Test-only bytes/clock/input adapter for the real experimental radio script.
local radio = dofile("tests/edgetx_distance_fixture.lua")
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
  local tick, sc, rud, ele, sb, takeover, packet = line:match(
    "^(%d+) ([%-]?%d+) ([%-]?%d+) ([%-]?%d+) ([%-]?%d+) ([01]) ([0-9a-f%-]+)$")
  assert(tick, "bad test step")
  r.sc, r.rud, r.ele, r.sb, r.native = tonumber(sc), tonumber(rud), tonumber(ele), tonumber(sb), takeover == "1"
  local value, fresh, sequence, heartbeat, pitch, pitchFresh = r:step(unhex(packet), tonumber(tick))
  local replies = {}
  for i = written + 1, #r.writes do replies[#replies + 1] = r.writes[i] end
  written = #r.writes
  io.write(string.format("%d %d %d %d %d %d %s\n", value, fresh, sequence, heartbeat,
    pitch, pitchFresh, hex(table.concat(replies))))
  io.flush()
end
