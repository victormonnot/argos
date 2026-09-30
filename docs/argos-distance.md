# Experimental apparent-distance assistance

`ARGOS DST` is a separate Pocket model and `python -m argos.distance` is a
separate launcher. Keep the working `ARGOS FLY` model, script and configuration.
This prototype regulates the selected person's apparent box height. It does
not measure metres, hold altitude, avoid obstacles or compensate for changing
body pose. A tilted aircraft also changes the camera measurement.

| Pocket selection | Behaviour |
| --- | --- |
| SC middle | Manual control |
| SC↑ | Existing bounded yaw assistance |
| SC↓ with SB↑ | Yaw plus experimental pitch |

Pause at SC middle before each enable or mode change. SC↓ requires both yaw
and pitch sticks near centre and captures a new size reference. The supported
aircraft setup must map SB↑ / CH6 low to **ANGLE**, verified at the receiver.
An SB change withdraws the experiment and requires another SC cycle. The saved
mapping alone is not a live flight-mode measurement.

Pitch is limited to ±51 Lua units, approximately 5% stick **before output
curves**; this is not a measured tilt angle or velocity. Throttle, roll and arming
remain manual. Roll input does not directly cancel either assistance. CH7 remains
fixed low, independently of SC. In distance mode, pitch stick input beyond 10%
temporarily gives the pilot pitch control while yaw assistance continues. Keep
this correction gentle: raw Rud takeover
applies in both assisted modes; raw Ele takeover also applies in distance mode:
more than 25% for 200 ms, or more than 50% immediately, followed by a latched
manual state requiring SC middle before re-enabling.

For a temporary pitch correction, recenter within 5% for 200 ms. Automatic pitch
then resumes toward the **original** size reference, using a fresh radio ticket
issued after recentering. This does not capture a new distance or reselect a
target. Old queued commands cannot restore pitch across this handoff. Native
logic independently releases pitch at the stick and holds that release until
the script has withdrawn pitch validity and provided it again. If Lua stalls
and entirely misses a brief stick gesture, that native hold can require a new
SC cycle; recentering alone must not revive frozen outputs.

Distance uses a filtered log-height error with measurement damping, a 6% log
size deadband, no integral term, and limited command slew. It withdraws pitch
for a clipped/off-centre box, low confidence, abrupt size change or markedly
changed aspect ratio. A short interruption needs two new suitable images;
repeated reads do not count. Target identity and image deadlines retain the
yaw validator's rules. The size reference changes only on an explicit enable,
never by silently adopting the most recent size during a gap.

## Prepare the separate radio artifact

Use an ordinary manual Pocket export, not an assisted/diagnostic model:

```sh
.venv/bin/python -m argos.backends.edgetx_distance_profile prepare \
  --source /path/to/manual.yml --script scripts/edgetx/ArgDst.lua \
  --output /path/to/new-distance-profile
```

With the aircraft disconnected, duplicate the ordinary model as `ARGOS DST`,
select the ordinary model, and connect USB Storage. Back up MODELS, RADIO and
the mixer scripts. Identify the new model by its header name and replace only
that inactive copy with the generated model. Install `ArgDst.lua` separately;
remove an obsolete compiled **ArgDst** cache if present. Do not replace ArgFly,
the model index, calibration or RADIO settings. Eject, restart and read back
the installed model/script before use.

Prepare a **separate** JSON configuration with the same camera, radio and Nano
model paths as FLY, but the verified distance profile path. Both services must
not own the camera/serial port at once. The experiment defaults to its own state
directory and refuses an occupied HTTP port before starting hardware workers.

```sh
.venv/bin/python -m argos.distance --config /path/to/distance.json --check
.venv/bin/python -m argos.distance --config /path/to/distance.json
```

The distinct `ARGOS_DISTANCE_STREAM_V2` handshake prevents the yaw-only or old
distance host from issuing commands. Status includes the pitch phase: `N` outside
distance mode, `A` automatic, `M` temporary manual, `R` waiting for a fresh command
after recentering. Automatic phase alone does not prove valid pitch output.
Commands carry separate yaw/pitch validity,
radio mode, session/generation and an expiring radio-issued ticket. Lines have
a 96-byte bound so maximum 31-bit counters plus both values fit. Invalid pitch
releases its native replacement even while valid yaw continues. Transport loss,
late tickets and a blocked producer cannot renew old authority.

## Required receiver check before the first flight

With propellers removed, verify the original manual channels and ANGLE mode.
Compare manual forward/back pitch reception with the automatic correction when
the selected person moves farther/closer. A height decrease must request the
same direction as manual forward pitch, and a height increase the opposite.
Check Ele takeover latches manual, SC middle releases both axes, and unplugging
Pocket USB restores manual pitch. This is a new pitch path; earlier yaw checks
do not validate its sign or mixer mapping.

After updating the temporary-pitch behavior, also check that a gentle pitch
correction reaches the receiver while yaw stays enabled, and recentering resumes
automatic pitch toward the unchanged reference. A strong gesture must still
latch manual. For a flight trial, establish a stable manual hover before enabling
SC↓: a ground-level size reference changes with camera framing during takeoff.

Software tests exercise the controller, local source, protocol, native profile
contract and actual Lua interpreter. They do not establish flight stability.
Return to the untouched FLY model and launcher for the established yaw demo.
