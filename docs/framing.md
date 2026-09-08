# Visual framing in GPS-free SITL

The optional **Visual framing** control uses a selected person's camera box to
request yaw, climb/descent and small forward/backward pitch inputs in AltHold.
It tries to center the person and retain their apparent height in the image.
**Closer** multiplies that reference by 1.1; **Farther** divides it by 1.1. Neither
button specifies a distance in metres.

This is an experimental simulation controller. It provides no position hold,
obstacle avoidance, trajectory planning or reliable identity recognition through
occlusions. Neutral inputs can leave horizontal drift. Changing a person's pose
can change the box height without any change in distance. See the actual
[validation status](validation.md) before treating it as demonstrated following.

## Start and operate

Prepare the pinned model, person asset and simulator described in
[vision.md](vision.md), then add the explicit framing option:

```sh
.venv/bin/python examples/run_web_control.py \
  --ardupilot-dir ../ardupilot \
  --gazebo-dir ../ardupilot_gazebo \
  --scene person \
  --vision-model "$HOME/.cache/argos/models/yolox_tiny.onnx" \
  --framing
```

The launcher requires the person scene and detector for this option. It forwards
`--sim-framing` to the console. The console requires opt-in simulation control,
the fixed Gazebo camera topic and a configured model. Default observation and
ordinary manual launches leave framing disabled. No physical control transport
is enabled by these flags.

1. Open **Flight controls**, enable **Person detection**, and **Take control**.
2. Prepare **AltHold**, arm and take off manually using the existing controls.
3. Click or tap the person's detection box. Selection alone does not move the
   vehicle. Wait for a recent, confident, fully visible detection of one person.
4. Release manual directions and select **Engage framing**. The autopilot must report
   armed AltHold and a fresh `IN_AIR` state; takeoff, landing and unknown landed
   states do not qualify. The current box height becomes the reference.
5. Use **Closer** or **Farther** while framing is active. The panel shows current
   and reference image heights as percentages, plus normalized centering errors.
6. Any manual direction or **Manual** stops assistance immediately. **Clear**
   removes a stopped selection. **Land** and **Release** retain their usual
   landing behavior. Another engagement always requires an explicit request.

Stabilize remains available for manual flight; framing requires AltHold. Mode
changes remain restricted to the ground. Turning Person detection off stops
framing. Leaving the flight view or losing browser/service context releases the
control lease as it does during manual flight.

## Target loss and operator priority

The controller requires an analyzed image received within **0.45 seconds**.
An empty detection frame or confidence below 0.5 immediately zeros corrections
and displays **Framing paused**. Only the same valid target returning within
**350 ms** can continue the current engagement. The pause deadline starts with
the first unusable detection and cannot be extended by later misses or browser
traffic. No previous box drives the vehicle during a pause; reference height is
retained, while command and derivative history are reset. Size adjustments are
unavailable during the pause.

Expiry of that pause, a changed track ID, multiple detected people, clipping,
unsupported box size, stale image, changed camera context or provider failure
latches a **two-second manual takeover** deadline with neutral outputs. Once
latched, even a returning detection cannot resume assistance. Engagement accepts
person heights from 8% to 45% of the image; active tracking allows 6% to 65%.

Select **Manual** or use a manual direction to acknowledge takeover. Neutral
browser keepalives, state polling and size adjustments do not acknowledge it.
Without acknowledgement by the deadline, the service revokes the lease and
requests Land, then stops pilot inputs and GCS heartbeats using the existing
fallback path. Browser input loss still has its independent **0.65-second**
lease timeout; process/link failure retains the pinned autopilot GCS failsafe.
These are landing requests, not a guarantee of horizontal braking or clearance.

Framing requests carry an increasing operator-intent number. An urgent Manual
request can overtake an old Engage request and invalidate it. Engagement also
checks the selected-target revision and the last acknowledged manual input
sequence. A late response cannot silently restore assistance after manual input.
If the browser cannot confirm Manual promptly, it releases the lease and asks
for landing instead of displaying an unconfirmed manual takeover.

## Controller inputs and limits

Only server-owned normalized boxes, confidence, image identity and local camera
receipt times enter framing. A click carries the displayed image/track identity;
the API does not accept a browser-supplied box. A bounded four-frame metadata
history tolerates a recently displayed frame, provided the target still exists
in the latest fresh analysis. There are no predicted boxes or automatic target
substitutions.

Horizontal image error requests yaw; vertical error requests climb or descent.
Forward pitch uses the log ratio of reference/current apparent height and a
filtered image-height derivative for damping. Reference changes do not create a
derivative kick. Large centering errors inhibit forward pitch while yaw and
vertical centering continue. Commands have deadbands, caps and slew limits; the
controller has no integral term. See [image_framing.py](../argos/guidance/image_framing.py).

The explicit digital profile removes RC stick/throttle deadzones so small
corrections reach ArduPilot. Framing-enabled sessions additionally check channel
mapping, reversal, RC calibration, yaw mapping, simple-mode settings and the
fixed camera's servo configuration. Unexpected values block arming and engagement;
a received profile change during armed control invokes the existing landing
path. The service reads parameters; it never silently rewrites a mismatch.
It spaces remaining/missing reads on the ground within a bounded startup window
to avoid overflowing the autopilot's parameter-response queue. Preparing the
mode before reads complete does not bypass the arming check.

With the pinned mapping, maximum framing requests are approximately 2.1 degrees
of pitch, 30.4 degrees/second of yaw and +0.30/−0.21 m/s of climb/descent. These
are autopilot demands, not measured motion or tracking-accuracy bounds. The
body-mounted camera makes pitch affect vertical framing as well as travel.
The controller does not compensate arbitrary camera mounts.

No GPS, downward optical flow, marker, known body size, simulator position,
depth or actor trajectory supplies controller feedback. The simulated barometer
and inertial sensors continue to support AltHold. This does not supply VIO,
GPS-free horizontal hover or a metric range estimate.

## API and journals

`POST /api/control/framing` requires the same Origin and owner token as the
manual API. Every body has `{token, operation, intent}`; intent is a positive,
strictly increasing integer within the current lease.

| Operation | Additional fields |
| --- | --- |
| `select` | `run_id`, `video_id`, `frame_sequence`, `track_id` from the displayed analysis |
| `engage` | `revision`, `input_seq` from acknowledged control state |
| `stop`, `clear`, `closer`, `farther` | None |

`control.framing` exposes phase, revision, selected target, eligibility/reason,
normalized errors, current/reference height, pause status, derived axes, frame receipt age and
remaining takeover time. Manual `control.axes` remains the browser's input;
neutral browser updates do not overwrite active derived commands. A valid newer
intent is consumed even if its requested transition is refused, preventing an
older request from taking precedence later.

Journals still contain received MAVLink frames only. Framing intents, outgoing
commands, images and detections are not recorded or replayed. Existing Sessions
views can inspect received vehicle telemetry from a framing flight, but cannot
reconstruct its complete visual-control history.
