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
fixed low, independently of SC. Moving an assisted axis beyond 10% of its
centre-to-stop travel temporarily gives that axis to the pilot, at **any**
amplitude or duration, including full deflection. Rud affects yaw in both
assisted modes; Ele affects pitch only in distance mode. Each axis is independent:
a pitch correction can keep yaw assisted, and a yaw correction can keep pitch
assisted.

Recenter the corresponding stick within 5% for 200 ms. Its automatic correction
then resumes using a fresh radio ticket issued after recentering, provided the
target and image are still valid. Pitch resumes toward the **original** size
reference; neither stick captures a new distance or reselects a target. A strong
movement no longer causes a latched manual mode in ARGOS DST. Use **SC middle**
for persistent manual control. The separate established ARGOS FLY model retains
its own original takeover behavior.

Old queued commands cannot restore an axis across this handoff. Native logic
independently releases each axis and holds that release until the script has
withdrawn its validity and provided it again. If Lua stalls and entirely misses
a brief stick gesture, the native hold can still require an SC cycle; recentering
alone must not revive frozen outputs. Communication, clock, malformed-input and
model faults retain their withdrawal/re-enable rules. Stick priority is not a
bypass of these rules.

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

The distinct `ARGOS_DISTANCE_STREAM_V3` handshake prevents the yaw-only or old
distance host from issuing commands. Status includes separate yaw and pitch
phases: `N` outside the corresponding assisted mode, `A` automatic, `M` temporary
manual, `R` waiting for a fresh command after recentering. Radio state `A` means
fresh perception commands are accepted, even while both sticks have priority;
it does not prove that either axis is assisted. Automatic phase alone does not
prove a valid native channel output.
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
Check stick priority reaches each channel, SC middle releases both axes, and unplugging
Pocket USB restores manual pitch. This is a new pitch path; earlier yaw checks
do not validate its sign or mixer mapping.

After updating stick priority, check full-deflection pitch and yaw corrections
reach their channels, that the other axis stays eligible, and that recentering
resumes correction without an SC cycle or reference change. One combined
props-off receiver sequence can cover those handoffs and SC manual release.
For a flight trial, establish a stable manual hover before enabling SC↓: a ground-level size reference changes with camera framing during takeoff.

Software tests exercise the controller, local source, protocol, native profile
contract and actual Lua interpreter. They do not establish flight stability.
Return to the untouched FLY model and launcher for the established yaw demo.
