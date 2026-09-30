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
remain manual. CH7 remains fixed low, independently of SC. Raw Rud takeover
applies in both assisted modes; raw Ele takeover also applies in distance mode:
25% for 200 ms, or 50% immediately, followed by a latched manual state.
Native L01–L19 logic independently guards stale script outputs and stick priority.

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

The distinct `ARGOS_DISTANCE_STREAM_V1` handshake prevents the yaw-only host
from issuing distance commands. Commands carry separate yaw/pitch validity,
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

Software tests exercise the controller, local source, protocol, native profile
contract and actual Lua interpreter. They do not establish flight stability.
Return to the untouched FLY model and launcher for the established yaw demo.
