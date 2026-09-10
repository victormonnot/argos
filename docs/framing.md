# Visual framing in GPS-free SITL

The optional **Visual framing** control uses a selected person's camera box for
bounded yaw and forward/backward pitch assistance. The panel offers three choices:

| Assistance | Required flight mode | Who controls vertical movement? |
| --- | --- | --- |
| **Manual** | AltHold or Stabilize | The pilot, through that mode's normal controls. |
| **Full framing** | AltHold | ARGOS requests climb/descent to center the person vertically. |
| **Framing + manual throttle** | Stabilize | The pilot uses the web throttle slider, +/− buttons or R/F keys. |

Both framing profiles try to retain the person's apparent height in the image.
**Closer** multiplies that reference by 1.1; **Farther** divides it by 1.1. Neither
button specifies a distance in metres. With manual throttle, ARGOS never requests
vertical correction: the pilot manages height and ground clearance. ArduPilot
still stabilizes attitude and applies its ordinary throttle mapping and tilt
compensation. These are web inputs in SITL; independent radio authority and a
Betaflight adapter are later integrations.

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

Tiny remains the default detector. To try the optional ground-computer S profile,
first run `examples/setup_vision_model.py --variant s`, then replace the model
path above with the printed S path and add `--vision-variant s`. See the
[model profiles](vision.md#model-and-data-flow) and their
[offline evidence](validation.md#optional-detector-profiles--september-9-2026).
On the evaluated ground computer, S needed `--vision-threads 4` to complete
the documented live framing trial; the default remains two threads. See the
[live checks and their limits](validation.md#live-s-framing-checks) before choosing
a profile. Freshness, clipping, identity and takeover rules remain unchanged.

1. Open **Flight controls**, enable **Person detection**, and **Take control**.
2. Prepare **AltHold** for Full framing, or **Stabilize** for manual-throttle
   framing. Arm and take off manually using the existing controls.
3. Click or tap the person's detection box. Selection alone does not move the
   vehicle. Wait for a recent, confident, fully visible detection of one person.
4. Release direction inputs and select the matching framing button. In Stabilize,
   continue managing throttle; it does not need to be zero or 50% to engage.
   The autopilot must report the prepared mode, armed, and a fresh `IN_AIR` state.
   The current box height becomes the reference.
5. Use **Closer** or **Farther** while framing is active. The panel shows current
   and reference image heights as percentages, plus normalized centering errors.
6. **Manual** or a roll/pitch/yaw direction stops assistance immediately. In Full
   framing, a manual climb/descent input also stops it. In manual-throttle
   framing, throttle changes preserve assistance; keep adjusting them as needed.
   **Clear** removes a stopped selection. **Land** and **Release** retain their
   usual landing behavior.

To change between framing profiles in flight, choose the other **Flight mode**,
release direction inputs, and use **Switch mode**. Wait for confirmation, then
select the person and explicitly engage the matching profile. Switching stops
framing and clears the selection. The transfer to Stabilize seeds the slider
from recent autopilot output; it does not assume hover at 50%. See
[mode transitions](web-control.md#change-mode-in-flight).

Manual throttle does not automatically center the person vertically. Large
vertical or horizontal image errors inhibit forward/backward correction in
both profiles, so Closer/Farther may wait until the pilot restores suitable
framing. Size changes cannot establish distance to the ground.

Turning Person detection off stops framing. Leaving the flight view or losing
browser/service context releases the control lease as during manual flight.

## Target loss and operator priority

The controller requires an analyzed image received within **0.45 seconds**.
Before evaluating flight control, the service collects an already completed
vision result without waiting for inference. This also happens after telemetry
polling, so a result completed during that work does not wait for another loop
iteration. The original image receipt time is retained: collecting a result
never makes an old image fresh or extends a takeover deadline.

An empty detection frame or confidence below 0.5 immediately zeros corrections
and displays **Framing paused**. In the manual-throttle profile, this zeros
ARGOS attitude corrections while the latest fresh pilot throttle continues. Only the same valid target returning within
**600 ms** can continue the current engagement. The pause deadline starts with
the first unusable detection and cannot be extended by later misses or browser
traffic. No previous box drives the vehicle during a pause; reference height is
retained, while command and derivative history are reset. Size adjustments are
unavailable during the pause. The confidence requirement remains 0.5 for both
engagement and continuation. Fresh analyzed images must keep arriving during this
interval: the independent 0.45-second image-age check still stops a frozen feed.
A longer pause never permits using a missing or predicted box for corrections.

The pause was increased from 350 to 600 ms after recorded same-person recovery
gaps of about 400–450 ms. See [validation](validation.md) for the comparison and
[offline replay](../examples/replay_framing.py) for a reproducible policy check.

A narrowly bounded overlapping-detection ambiguity can also enter that same
neutral pause: there must be exactly two boxes, the selected target must satisfy
its normal confidence and geometry checks, and the other box must be below 0.5
confidence and fully contained in the selected box. Both detections remain in
the observation and overlay. Containment does not prove a duplicate; a real
partially occluded second person can have this geometry too.

After this ambiguity, recovery requires **two consecutive distinct fresh images**
containing only the original selected ID. The first image keeps outputs at zero;
only the second can resume within the original 600 ms budget. Repeated reads do
not count as a second image. Another eligible bad observation clears the
confirmation count without extending the deadline. A confident or disjoint
second detection, missing selected ID, clipping, stale image or source/order
problem still triggers takeover. Engagement always refuses a multi-box image.
See the [recorded overlap replay](../examples/data/framing_overlap/README.md).

Expiry of that pause, a changed track ID, other multiple detections, clipping,
unsupported box size, stale image, changed camera context or provider failure
latches a **two-second manual takeover** deadline with neutral outputs. Once
latched, even a returning detection cannot resume assistance. Engagement accepts
person heights from 8% to 45% of the image; active tracking allows 6% to 65%.

Select **Manual** or use a manual direction to acknowledge takeover. In
manual-throttle framing, throttle changes alone do not acknowledge target loss:
the pilot retains gas control during the deadline and must explicitly take over
the other axes. Neutral browser keepalives, state polling and size adjustments
do not acknowledge it.
Without acknowledgement by the deadline, the service revokes the lease and
requests Land, then stops pilot inputs and GCS heartbeats using the existing
fallback path. Browser input loss still has its independent **0.65-second**
lease timeout; process/link failure retains the pinned autopilot GCS failsafe.
These are landing requests, not a guarantee of horizontal braking or clearance.

After a control interruption, both control panels retain the server's cause until
the next successful control claim or session change. A target-loss landing shows
the target failure followed by the expired manual-takeover deadline, including
when a late input request first returns an expired-owner error. This avoids
mistaking the consequence of a revoked lease for another browser taking control.

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

Horizontal image error requests yaw. Full framing converts vertical error into
climb/descent; manual-throttle framing fixes its derived vertical axis at zero
and sends the pilot's current throttle through the normal Stabilize input path.
Forward pitch uses the log ratio of reference/current apparent height and a
filtered image-height derivative for damping. Reference changes do not create a
derivative kick. Large centering errors inhibit forward pitch while yaw and
vertical centering continue in Full framing; the pilot manages height in the
manual-throttle profile. Commands have deadbands, caps and slew limits; the
controller has no integral term. See [image_framing.py](../argos/guidance/image_framing.py).

The explicit digital profile removes RC stick/throttle deadzones and checks
channel mapping, reversal and RC calibration for manual flight and framing.
Framing-enabled sessions additionally check yaw mapping, simple-mode settings
and the fixed camera's servo configuration. Unexpected values block arming and engagement;
a received profile change during armed control invokes the existing landing
path. The service reads parameters; it never silently rewrites a mismatch.
It spaces remaining/missing reads on the ground within a bounded startup window
to avoid overflowing the autopilot's parameter-response queue. Preparing the
mode before reads complete does not bypass the arming check.

With the pinned mapping, maximum framing requests are approximately 2.1 degrees
of pitch, 30.4 degrees/second of yaw and +0.30/−0.21 m/s of climb/descent. These
are autopilot demands, not measured motion or tracking-accuracy bounds. The
climb/descent figures apply only to Full framing in AltHold, not manual throttle. The
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
| `select` | `mode_generation`, `run_id`, `video_id`, `frame_sequence`, `track_id` from current control/displayed analysis |
| `engage` | `mode_generation`, `revision`, `input_seq` from acknowledged control state; optional `profile`: `full` (default) or `pilot_throttle` |
| `closer`, `farther` | `mode_generation` from current control state |
| `stop`, `clear` | None |

Mode-sensitive operations carry the current flight-mode generation. Omission
is accepted only at generation zero for legacy clients. A stale generation
returns the [typed mode conflict](web-control.md#control-api-and-recordings)
without changing the selection or advancing framing intent. Selection is also
refused during a mode transfer. This prevents a delayed pre-switch Select from
restoring the old target after returning to AltHold. Stop/Clear remain
unversioned so manual cancellation retains priority.

`control.framing` exposes the current `profile`, per-profile engagement eligibility
under `profiles`, phase, revision, selected target, eligibility/reason,
normalized errors, current/reference height, pause status, derived axes, frame receipt age and
remaining takeover time. `control.framing.last_loss` retains the first takeover's
cause, selected ID and bounded detection metadata through landing and disarming.
Its `at` is the takeover time; `evidence_at` identifies when the saved image
metadata was sampled. For an expired detection pause, the evidence is the first
unusable frame, even if another frame is available at expiry. A successful new
selection, engagement or lease clears this diagnostic. These are session receipt
times, not camera exposure times.

`control.interruption` retains the revoked lease's time, full reason and optional
framing loss until a new claim. Its `lease_started_at` matches the claim's public
`control.lease_started_at`; the browser uses this association to reject another
lease's diagnosis even when responses arrive out of order. It is a session time,
not the secret capability token. It survives later command errors and disarming;
`control.command` continues to report the landing command's separate outcome.
These bounded snapshots are available from state reads and, when enabled,
sampled by visual session recording.
Manual `control.axes` remains the browser's input;
neutral browser updates do not overwrite active derived commands. Stabilize
throttle updates leave target selection and framing intent intact, while still
superseding an older queued flight-mode handoff when their throttle changes. A valid newer
intent is consumed even if its requested transition is refused, preventing an
older request from taking precedence later.

The JSONL journal retains received MAVLink frames. Optional
[visual flight replay](console.md#visual-flight-replay) adds camera images,
matched detections, sampled framing/control state and discrete service requests.
It does not retain every outgoing command or establish exact aircraft motion.
No absent video, box, input update or measurement is reconstructed.

New visual captures retain the framing profile in sampled state and first-loss
evidence; Engage events name the manual-throttle variant. Replay displays the
recorded profile. Older archives without this field remain readable and their
profile is not inferred retroactively.

ArduPilot references: [Stabilize](https://ardupilot.org/copter/docs/stabilize-mode.html)
and [AltHold](https://ardupilot.org/copter/docs/altholdmode.html).
