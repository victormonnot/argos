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
