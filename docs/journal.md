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
