# Manual web flight in Gazebo/SITL

**Flight controls** lets an operator fly the simulated drone with held mouse or touch
buttons and optional keyboard shortcuts. The camera and current vehicle state
remain visible. ArduPilot stabilizes attitude in both supported modes; AltHold
also regulates altitude, while Stabilize uses manual throttle. The browser
supplies pilot input. This is an opt-in simulation feature, separate from the
default passive observation workflow.

Optional [visual framing](framing.md) adds explicit target selection and
image-based centering/apparent-size assistance in AltHold. The current milestone
does not implement VIO, position hold, radio control or HITL. It does not enable browser flight of
physical aircraft. No markers, downward optical flow or simulated true position
are substituted for a navigation capability.

## Launch an isolated session

Use Ubuntu 24.04, the ARGOS virtual environment and the pinned ArduPilot and
Gazebo Harmonic installations from the [SITL setup guide](sitl-observation.md).
Their binaries and models must already be built. From the ARGOS repository root:

```sh
.venv/bin/python examples/run_web_control.py \
  --ardupilot-dir ../ardupilot \
  --gazebo-dir ../ardupilot_gazebo
```

Replace the directory arguments if those installations live elsewhere. The
launcher starts Gazebo headlessly, ArduPilot SITL and ARGOS, then prints the run
directory and **http://127.0.0.1:8081**. Add `--gui` to request a Gazebo window on
a machine with a graphical session. Initialization can take several seconds.

The default ports are HTTP **8081/TCP**, MAVLink **5860/TCP** and physics
**9004/UDP**. Use `--port`, `--mavlink-port` and `--physics-port` to select free
ports for this session. The launcher reserves SITL instance 10 (including its
auxiliary ports); multiple copies of this launcher on one host are not supported.
It checks the three configured ports, creates a
private Gazebo partition and model copy, and uses fresh SITL parameter storage.
It preserves the installed models and other simulation sessions. The upstream
debug-coordinate visual is removed from the copied world.

Press **Ctrl-C** in the launcher terminal to stop its console, SITL and Gazebo
children. Their logs, copied world and SITL files remain in the printed run
directory for inspection. The usual ARGOS recording directory is retained.

The run's `processes.json` retains launcher and child PIDs, ports and partition.
Its `provenance` section also records the launcher arguments and working directory,
the requested Gazebo mode (`headless` or `gui`), and each directly launched child's
command and working directory. On Linux, it includes the boot ID and process
start ticks from `/proc` to help distinguish a process from a later reuse of its
PID. Unavailable identity fields are `null` and do not prevent startup.

These are startup records. They do not track later descendants, process
replacements or renderer selection, so compare them with the current process
tree and rendering log when investigating a failure. The manifest does not
measure memory use or establish long-duration simulation stability.

For access from another computer, forward the simulation's HTTP port through SSH:

```sh
ssh -N -L 8081:127.0.0.1:8081 user@host
```

Open the forwarded localhost address. The control surface supports touch;
access from a tablet still requires a route to this local service, such as an
SSH tunnel available on that device. ARGOS does not add a public server or
network authentication service. The [operating guide](running.md) covers SSH
and tmux. This SITL binary's MAVLink TCP listener binds all IPv4 interfaces and
accepts one client; the ARGOS console connects through loopback. Do not attach
QGroundControl or MAVProxy as a second client on that port.

For a walking person and image-based detection during manual flight, use the
optional [vision setup](vision.md). This adds camera observations without
changing flight authority or the GPS-free profile.

## Fly from the interface

If a MAVLink recording is wanted, start it in **MAVLink recording** before entering
Flight controls. The capture continues while flying.

1. Open **Flight controls**, next to Observation. Wait for the simulation and its
   disarmed vehicle state to become available.
2. Select **Take control**. One browser can own the
   controls at a time. Taking control starts neutral pilot inputs and checks the
   required simulation parameters; it does not arm or take off.
3. Wait for a fresh landed report, choose **AltHold** or **Stabilize** while
   disarmed, then select **Prepare** and wait for confirmation of that mode.
4. Select **Arm** with neutral axes and zero manual throttle. A new control
   lease always requires explicit preparation, even if the vehicle already
   reports the chosen mode. Normal ArduPilot arming checks remain enabled.
5. In **AltHold**, hold **Climb** to take off. In **Stabilize**, increase the
   **Held throttle** control progressively using its slider or +/− buttons. It
   starts at **0%** on arming: arming never applies an implicit 50% throttle.
6. Hold the direction or yaw buttons to maneuver. Multiple fingers can combine
   inputs. In Stabilize, releasing a direction preserves the chosen throttle
   while control remains active; it does not automatically hold altitude.
7. Once airborne, release directions, choose the other **Flight mode** and
   select **Switch mode**. Wait for confirmation from the drone. See the transfer
   behavior below; selecting an option alone does not change the autopilot mode.
8. Select **Land** to request ArduPilot's Land mode. After landing,
   **Disarm on ground** requires a fresh landed report. **Release** gives up the
   controls and requests landing when armed. A new lease starts with zero
   manual throttle; old throttle values are not restored.

| Mode | Roll/pitch | Vertical control |
| --- | --- | --- |
| AltHold | Self-leveling angle input | Held climb/descent input; neutral requests altitude hold. |
| Stabilize | Self-leveling angle input, similar to Angle mode | Explicit pilot throttle; no altitude regulation. |

The throttle percentage is normalized pilot input, not measured thrust, motor
RPM or a calibrated climb rate. ArduPilot applies its throttle mapping and tilt
compensation. AltHold's configured climb/descent speed limits do not limit
Stabilize's climb speed. Both modes remain GPS-free and can drift horizontally.

| Control | Optional keyboard shortcut | Input |
| --- | --- | --- |
| Forward / Backward | Up / Down arrow | Forward / backward inclination |
| Left / Right | Left / Right arrow | Left / right inclination |
| Climb / Descend (AltHold) | R / F | Climb / descend while held |
| Held throttle (Stabilize) | R / F | Increase / decrease the displayed pilot throttle by 2 percentage points per press |
| Yaw L / Yaw R | Q / E | Left / right yaw |

Inputs follow the vehicle's axes. Letter shortcuts use the labeled key on the
active keyboard layout. They are ignored when editing a field, using
Ctrl/Meta/Alt modifiers or outside Flight controls. The range control also supports its
native keyboard interaction. Pointer capture supports releasing direction
buttons outside their bounds; cancellation clears the affected held direction.
The throttle slider sets a value rather than a held climb command.

**Neutral is not stop or position hold.** Releasing directions levels the
attitude request but horizontal drift can continue. In Stabilize you must keep
adjusting throttle to control height. AltHold's vertical neutral seeks to hold
altitude; it does not observe or hold horizontal position.

## Change mode in flight

The browser supports an explicit **Stabilize ↔ AltHold** change after the
autopilot reports `IN_AIR`. It requires the current lease, a prepared current
mode, the checked simulation profile and released direction buttons. Framing
stops and its selection clears when a switch starts. Input buttons pause until
the new mode is confirmed by a received autopilot heartbeat; a command
acknowledgement alone does not enable them.

- **Stabilize → AltHold:** the service retains the last manual throttle through
  the transfer, then centers the altitude stick when AltHold is observed.
- **AltHold → Stabilize:** the service uses recent autopilot `ATTITUDE_TARGET`
  thrust and `MOT_THST_HOVER` receipts to invert ArduPilot's pilot-throttle
  mapping. The slider starts at that accepted value. It then remains under
  manual control: adjust it to maintain height.

This uses reported controller output and the autopilot's learned hover setting,
not simulator position or a calibrated motor-thrust measurement. It does not
assume that 50% pilot input is 50% thrust. Missing, old or incompatible mapping
information prevents the switch and is explained in the interface.

A single neutral-attitude bridge input precedes the mode command. Its throttle
is limited to 25–75% pilot input for this transfer. Pilot packets and GCS
heartbeats pause while waiting for the target mode; browser keepalives continue to renew
the usual 650 ms lease. A retained input can briefly mean climb/descent in
AltHold, bounded by the checked profile to −0.35/+0.5 m/s requested vertical
speed. The mode must be confirmed within one second. This limits the transfer
window; it does not guarantee an altitude-perfect or completely smooth handoff.
The bridge refreshes ArduPilot's GCS presence and RC override together. Pausing
both streams preserves the checked two-second GCS fallback before the
three-second override expiry if the subsequent Land command cannot be confirmed.

If transmission fails, the autopilot rejects the change, confirmation times
out or another mode appears, control is released and the existing Land/GCS
fallback applies. **Land** and **Release** remain available during the transfer.
No old stick packet or previous framing engagement resumes after a switch.

## What happens when control is lost

Leaving Flight controls, moving to another window, hiding or closing the tab, changing
the source, or losing current service state clears the held inputs and releases
the browser's control token. Opening Sources or another workspace also leaves
Flight controls. Returning does not resume old inputs or silently retake control.

The browser sends the latest axes and manual throttle every 100 ms with one input request in
flight. Changes are coalesced instead of queuing old movements. The service
expires a lease after **0.65 seconds** without a valid input update, checked on
its next tick. Ordinary state polling cannot renew that lease, and a late input
cannot revive it.

Each input response, including its JSON body, must reach the browser within
**500 ms**. A timeout does not prove that the service rejected the input: it may
have accepted it before the return path or browser stalled. The browser releases
control because confirmation is uncertain. Its local interruption message stays
visible when the service subsequently reports a generic release; a more specific
service-side loss still takes precedence.

Large state and control responses use gzip when the client accepts it. This
reduces repeated configuration and telemetry traffic without changing the JSON
contract or either deadline. Camera JPEGs and recording downloads keep their
existing representation. Compression cannot make an interrupted connection or
a stalled browser suitable for flight control.

If this repeats, finish the landing and compare the same interface on the
simulation computer with access through the tunnel. Check whether the browser
and computer are also freezing, and reduce unrelated load before explicitly
taking control again. A visible person does not establish that command responses
or the analyzed images are current. Do not increase the deadlines merely to
silence the interruption.

On release or expiry, the service requests neutral attitude and Land when armed
or when arm execution is uncertain, then clears its stored inputs and stops its
periodic pilot inputs and GCS heartbeat. Its one Stabilize handoff input retains
the last throttle value until Land takes over; it does not substitute 0% or 50%.
If the link or process itself is lost,
the pinned SITL profile provides the separate GCS failsafe configured for Land.
Its 3-second RC override lifetime exceeds the 2-second GCS timeout, so the
autopilot can enter Land before a missing override falls back to low RC throttle.
This does not change the service's 0.65-second browser lease.
These mechanisms request a descent; they are not horizontal braking, obstacle
avoidance or a guarantee that any arbitrary scene permits a landing.

An explicit **Land** request suppresses subsequent manual flight inputs and
GCS heartbeat while the browser continues renewing its lease. This leaves the
GCS fallback available even if that Land command is refused or never confirmed.
Only a new explicit ground preparation resumes pilot transmissions. Mode change, local transmission,
MAVLink acknowledgement and observed vehicle state are distinct states. Commands
are not queued for retry. Source replacement requires released controls and
confirmed disarming. After losing the link, reopening the same MAVLink source
is allowed once the lease is revoked: reception resumes passively, while armed
or uncertain flight state is retained. Reconnection does not restore authority.

## GPS-free simulation profile

[sitl-web-control.parm](../examples/sitl-web-control.parm) disables both GPS
instances, compass use, horizontal EKF position/velocity sources, downward
optical flow, rangefinding, precision landing and external visual navigation.
Altitude uses the simulated barometer. The autopilot retains its normal arming
checks and receives simulated inertial sensors through the Gazebo/JSON model.
The compass driver group remains enabled so its `USE` parameters load, but all
three compass-use flags and the EKF yaw source are zero. Yaw has no absolute
heading reference and can drift.
The initial `--home` coordinates place the simulated world; they do not enable
a GPS receiver or supply a horizontal controller with position.

The browser prepares AltHold or Stabilize, both without a GPS position requirement. Pilot
inclination and yaw inputs are capped at 30% of the MANUAL_CONTROL input range;
the profile additionally sets maximum tilt (20°), AltHold climb/descent (1/0.7 m/s)
and final landing speed (0.5 m/s), using the pinned firmware’s current parameter
names and units.
This bounds requests, not the resulting drift or distance traveled.

The control service reads the parameters in
[`REQUIRED_PARAMETERS`](../argos/console/control.py) before allowing arming.
It does not rewrite them or bypass a mismatch. It also requires recent selected
ArduCopter heartbeat and SIMSTATE receipts. SIMSTATE is used only as a simulation
presence check: its coordinates are not retained by the controller or used to
steer. This check is an accidental-hardware guard, not sender authentication.
Taking control also requests HEARTBEAT, EXTENDED_SYS_STATE and ATTITUDE_TARGET
at 5 Hz, retaining
the two-second heartbeat/landed receipt limits even when simulation runs below
real-time speed.
While armed in AltHold, the service reads the learned `MOT_THST_HOVER` value
every 0.5 seconds. Transfer admission requires thrust no older than 0.45 seconds
and a hover receipt no older than one second. These are local receipt ages.
The service also checks the throttle-channel mapping, quad frame and override
settings used by the transfer. It does not change the learned hover value.
The digital profile sets RC stick and throttle deadzones to zero. Framing-enabled
sessions add yaw, simple-mode and fixed-camera checks described in the
[framing guide](framing.md).

For an already prepared isolated instance, the equivalent opt-in console command
without a camera is:

```sh
.venv/bin/python -m argos.console \
  --sim-control --environment simulation \
  --mavlink-tcp 127.0.0.1:5860 --sequence-scope channel --port 8081
```

This command does not launch the simulators or apply the parameter profile. The
standard launcher sets up all three processes and adds the Gazebo camera.
Without `--sim-control`, the console does not transmit pilot commands. The flag
requires an explicit simulation environment and a `127.0.0.1` TCP target;
physical-device, UDP and serial control are not enabled.

## Control API and recordings

The local API uses JSON and the exact console Origin for all mutations.

| Endpoint | Request / result |
| --- | --- |
| `GET /api/state` | Includes `control`: availability, ownership, phase, vehicle state, checked profile, selected_mode (0/2), prepared, axes, throttle, mode_generation, mode_switch readiness, mode_transition, mode_transfer and latest command evidence. No token is exposed. |
| `POST /api/control/claim` | `{}` → `{token, control}`. Requires a disarmed, available simulation. |
| `POST /api/control/input` | `{token, seq, mode_generation, axes: {forward, right, up, yaw}, throttle}` → `{control}`. Axes are finite within `[-1, 1]`, throttle within `[0, 1]`, and `seq` increases strictly. |
| `POST /api/control/action` | `{token, action}` → `{control}`. Actions: `prepare`, `arm`, `land`, `disarm`, `release`. `prepare` accepts optional `mode`: integer `0` (Stabilize) or `2` (AltHold; default). |
| `POST /api/control/action` with `switch_mode` | `{token, action: "switch_mode", mode, mode_generation, input_seq}` → `{control}`. Requires the observed generation and an acknowledged neutral-direction input sequence not superseded by a maneuver or throttle adjustment. The service computes the destination throttle. |
| `POST /api/control/framing` | Explicit selection, engagement, manual takeover and apparent-size adjustment; see the [framing contract](framing.md#api-and-journals). |

Stabilize requires `up=0` and an explicit throttle in armed input updates.
Nonzero throttle is rejected while disarmed or in AltHold. Legacy AltHold
clients can omit throttle; new clients send it explicitly. Preparation requires
neutral input, confirmed disarming and a fresh landed report. The requested mode
must then be confirmed before arming. `prepared` belongs to the current lease;
an observed mode alone does not authorize arming. Airborne changes use
`switch_mode`; preparation remains ground-only. Unsupported modes and uncertain
flight state are rejected.

`mode_generation` starts at zero for a new lease and advances at transfer start
and completion. Legacy input can omit it only at generation zero. A stale
generation returns HTTP 409 with `code: "stale_mode_generation"`, `detail` and
the current `control` snapshot. That rejected input does not renew the lease or
change the accepted input sequence. The browser synchronizes and sends a fresh
neutral update; it does not replay the old movement or retry the mode action.
Other ownership/expiry failures retain their normal release behavior. Framing
selection, engagement and distance adjustments use the same generation fence;
manual Stop/Clear remain available without it.

While `mode_transition` is present, valid keepalives contain zero axes and the
unchanged source-mode throttle. The backend owns the bridge and does not apply
new stick values. Completion retains `mode_transfer` with its generation, source
and destination modes, completion time and accepted throttle. The UI seeds its
manual throttle once for that generation, then preserves subsequent operator
adjustments even when later snapshots still contain the transfer record.

`control.owned` means some browser owns the lease; possession of the token is
required to operate it. The browser keeps its token only in memory. The snapshot
clock `control.at` allows older replies to be discarded. HTTP success reports
the service's handling, not proof that a requested vehicle action executed;
the `command` fields separate transmission, acknowledgement and observation.

The JSONL journal records **received MAVLink frames only**. Optional
[visual capture](console.md#visual-flight-replay) adds received camera images,
matched detections, sampled control state and discrete operator-request events.
HTTP acceptance means the service handled a request, not that the aircraft
executed it. The sidecar is not an exhaustive wire-command log and cannot
reconstruct every stick update. Recording continues if the browser disconnects.

## Verification boundaries

Python tests cover the controller, source restrictions and API behavior.
[Browser tests](../tests/browser/control.spec.cjs) exercise held mouse input,
actual Chromium multitouch events, pointer cancellation, lost capture, keyboard
guards, stale ownership, delayed replies, service loss and several screen sizes.
They use isolated fixtures and do not constitute flight validation or testing on
a physical tablet. Integrated simulator and hardware evidence is recorded
separately in the [validation record](validation.md).
