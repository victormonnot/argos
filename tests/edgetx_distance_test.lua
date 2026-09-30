local radio = dofile("tests/edgetx_distance_fixture.lua")
local count = 0
local function eq(a,b) assert(a == b, "expected " .. tostring(b) .. ", got " .. tostring(a)) end
local function test(name, fn) fn(); count=count+1; print("ok - "..name) end
local function active(mode)
  local r=radio(); r:begin(); r:arm(mode or "D")
  r:set(1,80,true,nil,nil,mode == "Y" and 0 or 30,mode ~= "Y")
  eq(r.output[1],80); return r
end

test("manual never enables on startup and peers are distinct",function()
  for _,sc in ipairs({-1024,1024}) do
    local r=radio({sc=sc}); r:step("AB1 abc012ef\n"); eq(r:latestStatus().session,"00000000")
    r:begin(); r:set(1,80,true,nil,nil,30,true)
    eq(r.output[1],0); eq(r.output[5],0); eq(r:latestStatus().mode,"N")
  end
end)
test("separate yaw and distance modes and full bounds",function()
  local y=active("Y"); eq(y.output[5],0); eq(y.output[6],0); eq(y:latestStatus().mode,"Y")
  y:set(2,80,true,nil,nil,1,true); eq(y:latestStatus().sequence,1)
  local d=active(); eq(d.output[5],30); eq(d.output[6],1024); eq(d:latestStatus().mode,"D")
  d:set(2,-205,true,nil,nil,-51,true); eq(d.output[1],-205); eq(d.output[5],-51)
  d:set(3,205,true,nil,nil,52,true); eq(d:latestStatus().sequence,1) -- no periodic status emitted yet
  eq(d.output[5],-51)
end)
test("distance freshness may withdraw pitch independently",function()
  local r=active(); r:set(2,60,true,nil,nil,0,false)
  eq(r.output[1],60); eq(r.output[2],1024); eq(r.output[5],0); eq(r.output[6],0)
  r:set(3,0,false); eq(r.output[1],0); eq(r.output[2],0); eq(r.output[6],0)
  r:set(4,0,true,nil,nil,0,true); eq(r.output[2],1024); eq(r.output[6],1024)
end)
test("invalid yaw cannot sneak in pitch and stale sequence cannot overwrite",function()
  local r=active(); r:set(2,0,false,nil,nil,1,true); eq(r.output[5],30)
  r:set(1,50,true,nil,nil,-10,true); eq(r.output[5],30)
  r:set(2,0,true,nil,nil,1,false); eq(r.output[5],30)
end)
test("direct enabled mode changes withdraw and require middle",function()
  local r=active(); local before=r:latestStatus().generation
  r.sc=-1024; r:step(); eq(r.output[1],0); eq(r.output[5],0)
  eq(r:latestStatus().mode,"N"); assert(r:latestStatus().generation>before)
  r:step(nil,10); eq(r:latestStatus().state,"M")
  r:arm("Y"); r:set(2,70); eq(r.output[1],70); eq(r.output[6],0)
end)
test("ANGLE exit and unsupported start require manual rearm",function()
  local r=active(); r.sb=0; r:step(); eq(r:latestStatus().state,"F"); eq(r.output[6],0)
  r.sb=-1024; r:step(); eq(r:latestStatus().state,"F")
  r:arm("D"); r:set(2,80,true,nil,nil,30,true); eq(r.output[5],30)
  r.ele=200; r.sc=0; r:step(); r.sc=1024; r:step(); eq(r:latestStatus().state,"M")
end)
test("pitch stick triggers immediate or delayed latched manual",function()
  for _,axis in ipairs({"rud","ele"}) do
    local r=active(); r[axis]=513; r:step(); eq(r:latestStatus().cause,"P"); eq(r.output[6],0)
    r[axis]=0; r:step(); eq(r:latestStatus().state,"F")
    r:arm("D"); r:set(2,70,true,nil,nil,20,true); r[axis]=257
    r:step(nil,5); r:set(3,70,true,24,nil,20,true); eq(r.output[6],1024)
    r:step(nil,25); eq(r:latestStatus().cause,"P"); eq(r.output[6],0)
  end
  local y=active("Y"); y.ele=1024; y:step(); eq(y.output[1],80)
end)
test("native takeover and lease expiry withdraw both outputs",function()
  local r=active(); r.native=true; r:step(); eq(r:latestStatus().cause,"P"); eq(r.output[6],0)
  local e=active(); e:step(nil,30); eq(e:latestStatus().cause,"E"); eq(e.output[1],0); eq(e.output[5],0)
  e:step(nil,100); eq(e:latestStatus().state,"F")
end)
test("tickets expire on original issue and reconnect fences old commands",function()
  local r=active(); local old=r:latestStatus(); r:step(nil,31)
  r:set(2,90,true,nil,old,30,true); eq(r.output[6],0)
  r:begin("12345678"); r:set(3,90,true,nil,old,30,true); eq(r.output[6],0)
  r:step(nil,32); eq(r:latestStatus().state,"M")
end)
test("queued and oversized data fault before publishing multiple outputs",function()
  local r=active(); r:step(string.rep("x",97).."\n"); eq(r.output[6],0); eq(r:latestStatus().cause,"O")
  local q=active(); q:step(string.rep("x",384)); eq(q.output[6],0); eq(q:latestStatus().state,"F")
end)
test("two accepted queued commands publish newest once",function()
  local r=active(); local hb=r.output[4]
  r:step(r:command(2,20,true,nil,10,true)..r:command(3,30,true,nil,-10,true))
  eq(r.output[1],30); eq(r.output[5],-10); eq(r.output[4],-hb)
end)
test("clock wrap supports pitch leases with Lua32 integers",function()
  local r=radio({now=2147483630}); r:begin(); r:arm("D"); r:set(1,80,true,nil,nil,30,true)
  r:step(nil,-2147483646); eq(r.output[6],1024)
  r:step(nil,-2147483636); eq(r.output[6],0)
end)
test("malformed peripheral and clock states withdraw all six outputs",function()
  for _,change in ipairs({function(r) r.ele=nil end,function(r) r.sb=99 end,
      function(r) r.name="ARGOS FLY" end,function(r) r.flip=1024 end,
      function(r) r.clockError=true end,function(r) r.readError=true end}) do
    local r=active(); change(r); r:step(); eq(r.output[1],0); eq(r.output[5],0); eq(r.output[6],0)
  end
end)
print("passed "..count.." distance Lua tests")
