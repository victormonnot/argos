# Manual web flight in Gazebo/SITL

**Pilotage** lets an operator fly the simulated drone with held mouse or touch
buttons and optional keyboard shortcuts. The camera and current vehicle state
remain visible. ArduPilot runs the attitude and altitude loops; the browser
supplies pilot input. This is an opt-in simulation feature, separate from the
default passive observation workflow.

The current milestone does not implement visual following, target lock, VIO,
position hold, radio control or HITL. It does not enable browser flight of
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

## Fly from the interface

If a MAVLink recording is wanted, start it in **Journal MAVLink** before entering
Pilotage. The capture continues while flying.

1. Open **Pilotage**, next to Observation. Wait for the simulation and its
   disarmed vehicle state to become available.
2. Select **Prendre les commandes** (take control). One browser can own the
   controls at a time. Taking control starts neutral pilot inputs and checks the
   required simulation parameters; it does not arm or take off.
3. Select **Préparer (AltHold)** and wait for the reported mode confirmation.
   Then select **Armer** and wait for confirmed arming. ArduPilot's arming checks
   remain enabled; a rejected or unconfirmed request is shown explicitly.
4. Hold **Monter** to take off manually. Hold the directional or rotation buttons
   to maneuver. A button acts only while pressed. Multiple fingers can combine
   axes, for example climbing while turning.
5. Descend manually with **Descendre**, or select **Atterrir** to request
   ArduPilot's Land mode. After landing, **Désarmer au sol** requires a recent
   landed-state report. **Libérer** gives up the controls; it requests landing
   when the drone is armed.

| Held button | Optional keyboard shortcut | Input |
| --- | --- | --- |
| Avant / Arrière | Up / Down arrow | Forward / backward inclination |
| Gauche / Droite | Left / Right arrow | Left / right inclination |
| Monter / Descendre | R / F | Climb / descend |
| Rotation G / Rotation D | Q / E | Left / right yaw |

Inputs follow the vehicle's axes. The letter shortcuts use the labeled key on
the active keyboard layout. They are ignored when editing a field, using
Ctrl/Meta/Alt modifiers, or outside Pilotage. Pointer capture supports releasing
outside a button; pointer cancellation and lost capture clear the affected input.
Touch gestures are suppressed on movement buttons, while the rest of the page
retains ordinary scrolling.

**Neutral is not stop or hover.** Releasing a direction returns its input to
neutral, but horizontal drift can continue. The flight mode does not observe or
hold horizontal position. The climb control's neutral input requests altitude
holding through AltHold; this is not a precise position-hold claim.

## What happens when control is lost

Leaving Pilotage, moving to another window, hiding or closing the tab, changing
the source, or losing current service state clears the held inputs and releases
the browser's control token. Opening Sources or another workspace also leaves
Pilotage. Returning does not resume old inputs or silently retake control.

The browser sends the latest four axes every 100 ms with one input request in
flight. Changes are coalesced instead of queuing old movements. The service
expires a lease after **0.65 seconds** without a valid input update, checked on
its next tick. Ordinary state polling cannot renew that lease, and a late input
cannot revive it.

On release or expiry, the service clears its inputs, attempts a neutral input
and Land request when armed or when arm execution is uncertain, and stops its
periodic pilot inputs and GCS heartbeat. If the link or process itself is lost,
the pinned SITL profile provides the separate GCS failsafe configured for Land.
These mechanisms request a descent; they are not horizontal braking, obstacle
avoidance or a guarantee that any arbitrary scene permits a landing.

An explicit **Atterrir** request suppresses subsequent manual flight inputs
while the browser continues renewing its lease. Mode change, local transmission,
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

The browser requests AltHold rather than a GPS-dependent position mode. Pilot
inclination and yaw inputs are capped at 30% of the MANUAL_CONTROL input range;
the profile additionally sets maximum tilt (20°), pilot climb/descent (1/0.7 m/s)
and final landing speed (0.5 m/s), using the pinned firmware’s current parameter
names and units.
This bounds requests, not the resulting drift or distance traveled.

The control service reads the twelve parameters in
[`REQUIRED_PARAMETERS`](../argos/console/control.py) before allowing arming.
It does not rewrite them or bypass a mismatch. It also requires recent selected
ArduCopter heartbeat and SIMSTATE receipts. SIMSTATE is used only as a simulation
presence check: its coordinates are not retained by the controller or used to
steer. This check is an accidental-hardware guard, not sender authentication.
Taking control also requests HEARTBEAT and EXTENDED_SYS_STATE at 5 Hz, retaining
the two-second receipt limit even when simulation runs below real-time speed.

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
| `GET /api/state` | Includes `control`: availability, ownership, phase, vehicle state, checked profile, axes and latest command evidence. No token is exposed. |
| `POST /api/control/claim` | `{}` → `{token, control}`. Requires a disarmed, available simulation. |
| `POST /api/control/input` | `{token, seq, axes: {forward, right, up, yaw}}` → `{control}`. Every axis is finite and within `[-1, 1]`; `seq` must increase strictly. |
| `POST /api/control/action` | `{token, action}` → `{control}`. Actions: `prepare`, `arm`, `land`, `disarm`, `release`. |

`control.owned` means some browser owns the lease; possession of the token is
required to operate it. The browser keeps its token only in memory. The snapshot
clock `control.at` allows older replies to be discarded. HTTP success reports
the service's handling, not proof that a requested vehicle action executed;
the `command` fields separate transmission, acknowledgement and observation.

ARGOS journals still record **received MAVLink frames only**. Received mode,
arming, acknowledgements and status reports can be inspected in Sessions, but
outgoing pilot commands and their browser timing are not recorded. Video is not
recorded either. A replay therefore cannot reconstruct the exact operator input
sequence or absent video. Recording continues if the browser disconnects.

## Verification boundaries

Python tests cover the controller, source restrictions and API behavior.
[Browser tests](../tests/browser/control.spec.cjs) exercise held mouse input,
actual Chromium multitouch events, pointer cancellation, lost capture, keyboard
guards, stale ownership, delayed replies, service loss and several screen sizes.
They use isolated fixtures and do not constitute flight validation or testing on
a physical tablet. Integrated simulator and hardware evidence is recorded
separately in the [validation record](validation.md).
