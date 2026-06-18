# Dev journal

One line per discovery, surprise, or trap. Raw and chronological — this is the
memory cement: interview material, build-in-public content, and my own notes.

---

## 2026-06-13 — Setup & first SITL flight

- **SITL is the *real* firmware.** ArduCopter's actual C++ code, compiled for the
  PC instead of the STM32, with sensors replaced by a physics model. Everything
  learned here (modes, params, safety checks) transfers 100% to real hardware.
- **MAVProxy** = a command-line ground station that launches with the SITL. Its
  prompt shows the current flight mode (`STABILIZE>`, `GUIDED>`...).
- **Topology:** commands go *up* (to the drone), telemetry comes *down*. Already
  the real drone's topology — orders up, data down.
- **Flight modes are contracts on who holds the stick.** A scripted takeoff only
  works in **GUIDED** — the mode where a computer is allowed to command. Tried
  `takeoff` in STABILIZE → refused.
- **Pre-arm checks:** arming is refused until the EKF has converged (GPS 3D fix,
  ~30 s after boot). Read the refusal, don't retry blindly — same check that
  protects the real drone. (Note: this SITL has `ARMING_CHECK` disabled, so
  arming was instant here. Real hardware won't be that forgiving.)
- **The ground station is a window, not the brain.** Closing QGroundControl
  mid-flight doesn't stop the drone — it keeps flying. Big lesson for ARGOS:
  when the Mode B link drops, the drone must have its own safe behavior.

## 2026-06-14 — First scripted mission (`mission_basic.py`)

- **Three MAVLink message families that matter:**
  - `HEARTBEAT` — link pulse (~1 Hz); tells you who you're talking to and the
    current mode. Used to open the link and confirm mode changes.
  - `COMMAND_LONG` / `COMMAND_ACK` — one-shot actions (arm, takeoff, set mode):
    request → response. Check `result == 0` (accepted).
  - `SET_POSITION_TARGET_LOCAL_NED` — the GUIDED setpoint ("go there"). No ack;
    you confirm arrival by reading `LOCAL_POSITION_NED`.
- **Closed loop beats `sleep()`.** A script doesn't watch QGC — each move is
  confirmed by *reading telemetry in a loop* until reality meets the setpoint.
  This is the embryo of ARGOS's perception→decision loop.
- **NED trap:** Down is positive toward the ground, so **altitude is negative**
  (`z = -10` for 10 m). Standard aero convention, not an ArduPilot quirk. ~90% of
  first GUIDED bugs come from `z = +10`.
- **`type_mask`:** each bit set to 1 means "ignore this field". Position-only =
  `0b110111111000` (keep x,y,z, ignore velocity/accel/yaw). Must be able to
  defend this, not copy a magic number.
- **Setpoint lifetimes:** a *position* target persists (drone goes and stays); a
  *velocity* target expires after ~3 s without refresh. That timeout is exactly
  the safety net for Mode B's continuous yaw-rate stream later.

### Traps debugged (the real learning)

- **Edited file ≠ run file.** After moving `mission_basic.py` into `sitl/`, the
  editor still had the *old* root path open; saving recreated a stray copy at the
  repo root while I kept running the `sitl/` one. "My changes aren't taken into
  account" is almost always this, or an unsaved buffer. **The file you edit and
  the file you run must be the same one.**
- **The SITL battery drains across a long session** and does *not* recharge
  between runs. After ~17 min and many flights, capacity hit 0%; the sim then
  drops available thrust, the drone physically *can't climb to the target*, and a
  wait-loop with no timeout **hangs forever** (stuck at 12.7 m waiting for 30 m).
  Two fixes: (1) restart the SITL → fresh battery; (2) **every wait-loop needs a
  timeout**. Added `TimeoutError` + a safety `LAND` fallback to `takeoff()` and
  `goto()` — "any in-flight failure → safe action".

### Sim environment

- **Default SITL home = Canberra** (CMAC model-aircraft field). Changed the
  default to **Toulouse** by adding an entry to ArduPilot's `locations.txt` and
  launching via `sitl/run_sitl.sh` (`--location Toulouse`). The mission code is
  unchanged: **NED is relative to home**, so the trajectory flies the same wherever
  home is — you just move the scenery under the drone.

### Yaw control (`yaw_demo.py`) — the ARGOS Mode B primitive

- **Commanding *where the drone looks* is Mode B in miniature.**
  `MAV_CMD_CONDITION_YAW` (COMMAND_LONG) commands a heading; `ATTITUDE` streams the
  real orientation (yaw in radians → heading 0–360°). Same closed loop as `goto()`:
  send the setpoint, loop on `ATTITUDE` until real heading meets target. Today the
  headings are hardcoded; in S5 the heading will come from a video detection.
- A multirotor **yaws in place** — it rotates about its vertical axis, no
  translation needed. ArduPilot only executes CONDITION_YAW when **armed & airborne**.
- **Trust the log, not the eyeball.** `yaw_demo` looked like it did nothing in QGC —
  but it worked perfectly: `ATT.DesYaw` (commanded) vs `ATT.Yaw` (actual) swept
  0→90→180→270→0 within ~1°. Two illusions fooled the eye: a *vertical* takeoff
  doesn't move the icon on the 2D map (looks like "no takeoff"), and a rotation that
  returns to North is easy to miss. **The DataFlash log is ground truth; the eye is not.**

### Networking — telemetry needs a reachable return address

- The SITL **pushes** UDP telemetry *to the Mac's IP*. A LAN IP (`192.168.x`) breaks
  the instant you change WiFi → QGC shows "disconnected". A **Tailscale IP** (`100.x`)
  is stable everywhere, so `run_sitl.sh` now targets that. SSH/terminal kept working
  the whole time because that's the Mac *connecting to* the fixe (inbound), not the
  outbound telemetry. **Same addressing problem will hit the real drone link**
  (DroneBridge WiFi) in S3.

### ArduPilot architecture — the scheduler *is* the firmware

- A flight controller isn't sequential code, it's a **cooperative real-time
  scheduler**. The whole firmware = one task table (`ArduCopter/Copter.cpp`
  `scheduler_tasks[]`), each task = `(function, rate_Hz, max_time_µs, priority)`.
  A 400 Hz base loop decides each tick which tasks are due and fits them into the
  remaining time budget.
- **Two tiers:** `FAST_TASK` = every loop, in order (IMU → rate controllers →
  motor output → EKF `read_AHRS`) — the inner loop that's never starved.
  `SCHED_TASK` = rate-limited, run by priority when there's time budget.
- **`max_time_µs` is the point:** every task has a bounded time, so a slow task
  can't starve the fast loop. *Real-time = predictable/bounded timing, not "fast".*
- **ARGOS lives in this table:** `AP_OpticalFlow.update` @200 Hz (the MTF-02P /
  GPS-denied hover), `read_AHRS` = EKF3 in the fast tier (S3 subject), and
  `GCS update_receive`/`update_send` @400 Hz = **where every MAVLink message I send
  and read enters and leaves the firmware**. My Python closed loop and this table
  are two halves of the same loop.

## 2026-06-16 — S2 start: C++ / MAVSDK guidance

- **MAVSDK is pymavlink's ergonomic layer.** Same MAVLink underneath, but where
  pymavlink had me craft a raw `COMMAND_LONG`, MAVSDK gives `action.arm()` /
  `action.takeoff()` / `action.land()`, and reads via a `Telemetry` plugin.
  Ported `mission_basic.py` → `guidance/src/main.cpp` (connect → arm → takeoff →
  land), built with CMake against `MAVSDK::mavsdk`.
- **The guidance law is a pure, unit-tested function.** `yaw_rate_command(error,
  kp, max_rate)` = a proportional controller with saturation — the heart of Mode B.
  Isolated in `control_law.hpp`, tested with doctest + CTest (6 assertions green),
  **no drone needed**. Principle: *prove the control law in unit tests before it
  ever touches the aircraft* — safety = hiring argument.
- **Continuous yaw-rate via MAVSDK Offboard** (`yaw_track.cpp`): stream
  `set_velocity_body({0,0,0, yawspeed})` at 10 Hz, the yaw-rate computed by the law
  from the heading error. Today error = (target_heading − current_heading), simulated;
  in S5 error = the detection's horizontal pixel offset. Same law, same loop — only
  the error source changes. (Velocity setpoints must be streamed — they expire ~3 s.)

## 2026-06-16 — S2 perception: baseline + VisDrone pipeline

- **GPU pipeline proven:** YOLO11n inference on `cuda:0` (RTX 4060). Dedicated venv
  at `perception/.venv` (torch 2.12+cu130).
- **VisDrone → 2 classes (personne/véhicule).** Ultralytics auto-downloads + converts
  to 10-class YOLO labels under `datasets/VisDrone/labels/{train,val,test}`; a remap
  pass collapses them (pedestrian+people→0, car/van/truck/bus/motor/tricycles→1,
  bicycle dropped). 8629 files, 444k boxes kept, 13k dropped.
- **Verify the pipeline, don't trust exit-0.** First remap touched **0 files** —
  my glob assumed `labels/*.txt` but the real layout is `labels/val/*.txt` (nested),
  and the dataset yaml structure differed from my guess. Caught only by *inspecting
  the actual files* after running. Fix: remap non-destructively (back up the pristine
  10-class labels once, always remap from the backup) so the class mapping can be
  changed without re-downloading.

## 2026-06-17 — S2 perception: TensorRT export + quantization benchmark

- **Pipeline:** `best.pt` → ONNX → TensorRT FP32/FP16/INT8 (`export.py`), INT8 via
  PTQ calibration on VisDrone; benchmarked mAP + latency p50/p95 + FPS per precision
  (`benchmark.py` → `benchmark.md`). TensorRT 11.1, RTX 4060, batch-1, imgsz 640.
- **Numbers:** PyTorch FP32 mAP50 0.527 / p50 6.6ms / p95 15.4ms; TRT FP32 0.526 /
  5.5 / 6.6; **TRT FP16 0.527 / 5.0 / 6.2 (198 FPS) — the sweet spot**; TRT INT8 0.507
  / 5.4 / 7.6.
- **The real lesson — measure, don't assume.** TensorRT's biggest win here isn't the
  mean speed, it's the **p95 tail collapsing 15.4 → 6.6 ms** (predictable latency
  matters more than peak FPS in embedded). And **INT8 lost**: −2 mAP points *and* not
  faster than FP16. Why: a yolo11n at 640/batch-1 already runs in ~5 ms → it's
  overhead-bound, not compute-bound, so INT8 has nothing to win; plus TRT fell back to
  mixed precision on the (de)quant layers (the `Skipping tactic` errors at build).
  Conclusion: **FP16 is the deployment choice**; INT8's case is on the Jetson Orin
  (DLA, different compute profile) or batched throughput — an honest S3 line, and a
  stronger interview story than a fake "INT8 ×3".

## 2026-06-17 — ArduPlane: GUIDED Copter vs Plane (Black Bird wink)

- Flew ArduPlane SITL in GUIDED via QGC click-to-go. The differences from Copter:
  - **No hover.** A plane can't stop — it **orbits** a GUIDED point (the "circles");
    `goto` on a plane = fly there and loiter around it, not arrive-and-hold.
  - **Takeoff needs airspeed** (roll / launch), not a vertical `NAV_TAKEOFF`.
  - **No vertical LAND.** Landing is an **approach sequence** (glide slope, DO_LAND_START);
    RTL just loiters over home. To end a sim flight: `disarm force`.
  - Constraints a multirotor doesn't have: **turn radius** (bank-limited), minimum airspeed.
- Takeaway: fixed-wing terminal guidance is **approach geometry + energy management**, not
  "position over the target" — the multirotor/fixed-wing split that Black Bird embodies.
- **C++ port (`guidance/src/plane.cpp`, mirrors `main.cpp`): the SDK abstraction leaks.**
  MAVSDK `action.takeoff()` works for ArduCopter but is **rejected on ArduPlane** (MAVSDK is
  PX4-shaped); even a raw `NAV_TAKEOFF` in GUIDED was rejected. Had to drop to **raw MAVLink**
  (`MavlinkPassthrough`) the ArduPilot way: **takeoff is a MODE, not a command** — arm + switch
  to mode `TAKEOFF` (13) → auto climb-out; end with mode `RTL` (11) → loiter (verified: alt ~50 m,
  mode RTL). Lesson: high-level SDKs are vendor-shaped; off the happy path you go back to the protocol.

## 2026-06-17 — Mode A: operator detection console (web)

- Built the operator-facing console (`perception/console.py`): video → FP16 TensorRT
  inference → OpenCV HUD → **MJPEG web stream** (FastAPI) viewable in a browser. Web
  (not `cv2.imshow`) so it needs no X display — works over SSH/WSL, reachable from the
  Mac via Tailscale. Core decoupled from display; `annotate()` + the HTML are the seam
  Victor refines. ~47 FPS on 1080p, ~40 detections/frame, vehicles at 0.9 conf.
- **UX lesson (Victor's call, correct):** a "video" stitched from independent VisDrone
  images is **unwatchable for a human** — it sabotages the whole point of a good operator
  UI. A real continuous aerial clip is essential. Fix: `get_video.py` now downloads a
  real aerial clip (Pexels, free) with the VisDrone-stitch only as a fallback. Good
  operator UX needs footage a human can actually follow, not just data the model likes.

## 2026-06-17 — Mode B: closed-loop visual yaw tracking (SITL)

- Wired the full loop in the console: click a detection → **lock** → **error** (offset from
  viewport centre) → **proportional law** (same as `control_law.hpp`, ported to Python) →
  **yaw-rate streamed via MAVLink** (`SET_POSITION_TARGET_LOCAL_NED`, velocity+yaw_rate mask)
  to the SITL ArduCopter → drone yaws.
- **Closed the loop with a simulated camera.** SITL has no camera (physics only), so a
  **viewport** (crop) pans over the recorded video, driven by the drone's heading (`ATTITUDE`).
  Drone yaws to centre the target → viewport pans → target re-centres → error → 0 → yaw settles.
  Verified: error +0.12 → ~0 in ~1 s, heading settled in a ~15° band tracking a moving car —
  vs open-loop (no pan) where it ran away 123° in 3 s.
- **Why not a 3D sim (Gazebo/AirSim)?** It would give a real rendered camera, but feed the
  detector **synthetic** images it wasn't trained on (domain gap). The viewport-pan keeps the
  detector on **real footage** (its domain). The true visual loop (real camera, flying drone)
  is **S5**. Residual hunting = naive nearest-neighbour tracker + moving target (ByteTrack later).
- Infra: connected straight to the SITL binary on **tcp:5760** (no MAVProxy headless) → had to
  `request_data_stream` manually; pre-arm checks disabled (SITL only) for a reliable takeoff.

## 2026-06-17 — Gazebo visual sim: infra UP, SITL↔Gazebo handshake PENDING

Goal: a real drone-in-3D-sim demo (free, no hardware) — camera-in-the-loop closed guidance,
the software version of S5. Chose **Gazebo Harmonic + ArduPilot SITL**.

- **Working ✅:** Gazebo Sim 8.13 installed; `ardupilot_gazebo` plugin **built** at
  `~/argos-project/ardupilot_gazebo/build` (deps: libgz-sim8-dev, libopencv-dev, gstreamer-1.0
  /-app, rapidjson). **Headless GPU rendering confirmed** (`GALLIUM_DRIVER=d3d12` →
  `D3D12 (RTX 4060)`). `iris_runway.sdf` loads `iris_with_gimbal` (a drone + camera); the
  **camera image topic** and IMU topics exist; the plugin opens FDM port **9002**.
- **Blocker ❌:** ArduCopter SITL (`--model gazebo-iris --sim-address=127.0.0.1`) does **not
  sync** with Gazebo headless — no MAVLink heartbeat. Root cause: the headless `gz sim -s -r`
  server **isn't stepping the physics** (IMU never publishes), so the ArduPilot↔Gazebo
  **lockstep deadlocks** (Gazebo waits for servos, SITL waits for state). Unpausing via the
  `/world/iris_runway/control` service (returned `data: true`) did **not** bootstrap stepping.
- **Leads for next session:** (1) run Gazebo **with the GUI on the physical PC** (WSLg display)
  and press play → confirms SITL+Gazebo flight works there, isolating the *headless-stepping*
  issue; (2) investigate headless server stepping (ogre2/EGL render engine flags, world
  `<physics>` + lockstep config); (3) double-check FDM port roles. Everything except the
  physics handshake is proven.
- **Retry on a CLEAN WSL (after a full reboot):** ruled OUT the process-conflict theory — a
  *bare* SITL now heartbeats fine, so the earlier "internal clock bits / Time has wrapped" was a
  leftover ArduPlane SITL + MAVProxy fighting over the clock (killed by the reboot). Ruled OUT
  lockstep (`lock_step=0` → no change). Gazebo **does step** (IMU publishes) — but note headless
  `gz sim -s -r` starts **PAUSED**; must unpause via the `/world/iris_runway/control` service.
  The SITL still blocks at `Home:`, never receiving FDM state on its bound port 9003. **Narrowed
  to: the plugin's FDM state never reaches the SITL** → most likely a **version mismatch**
  (`ardupilot_gazebo` cloned at latest master vs ArduPilot cloned in S1; the SITL JSON/FDM packet
  format may have changed). **Next: match versions** — update ArduPilot + rebuild SITL, or checkout
  a plugin tag matching his ArduPilot (see the ardupilot_gazebo README compat table).

## 2026-06-19 — Gazebo handshake SOLVED ✅ (real drone flies in the 3D sim)

The version-mismatch theory was **wrong** (both recent: ArduPilot 4.6.0-beta1 @2026-06-13,
plugin @2026-04-03, Gazebo 8.13). Three real causes, all fixed:

1. **Wrong model flag.** Launches used `--model gazebo-iris` (invalid) instead of
   **`--model JSON:127.0.0.1`**. The JSON FDM backend (`libraries/SITL/SIM_JSON.cpp`) sends
   servos to `127.0.0.1:9002` and the plugin **replies to the sender's address** (no fixed
   `fdm_port_out` in the SDF), so the bound-port worry was a non-issue.
2. **Editing the wrong SDF.** Yesterday's `lock_step` tweaks were on
   `models/iris_with_ardupilot/model.sdf`, but `iris_runway.sdf` loads **`iris_with_gimbal`**,
   whose **own** `ArduPilotPlugin` (model.sdf:187, `fdm_port_in 9002`, `lock_step 1`) is the
   active one. The edits had zero effect.
3. **"Headless doesn't step" was false.** `gz sim -s -r` **does** advance sim time headless
   (`/stats` real_time_factor ≈ 0.47, GPU ogre2 render). No unpausing needed.

**Proof it flies:** connected pymavlink to `tcp:5760` → HEARTBEAT (ArduPilotMega), full FDM
telemetry from Gazebo (RAW_IMU acc=(0,0,-1000)=gravity, GPS 3D fix 10 sats from the navsat
sensor), then **GUIDED → arm → NAV_TAKEOFF 10 m** and altitude climbed
`0.01→0.70→2.61→5.37→7.80→9.53→10.03→10.00 m` and held. Physics is 100% Gazebo responding to
ArduPilot's motor outputs. The "crashes" mid-debug were my own foreground kills (the sandbox
SIGs long foreground procs at 144) + the client disconnecting — the SITL itself is rock-solid.

**Reproducible:** `sitl/run_gazebo.sh` (launches Gazebo + SITL with the right wiring, clean
Ctrl-C teardown) and `sitl/gazebo_takeoff_test.py` (the arm→takeoff smoke test above).
Active camera topic for the next step:
`/world/iris_runway/model/iris_with_gimbal/.../camera/image`.

**Next:** bridge that Gazebo gimbal camera into `console.py` (subscribe via `gz topic` /
ros-gz or the gstreamer UDP the plugin can emit) → run the detector on the synthetic frames
(COCO weights for the synthetic domain) → close the **real** visual yaw loop, retiring the
viewport-pan hack.
