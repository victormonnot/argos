# Dev journal

One line per discovery, surprise, or trap. Raw and chronological.

## Week 1 — SITL

- **Setup done:** ArduPilot cloned, SITL `arducopter` built from source, MAVProxy + pymavlink installed, QGroundControl connected from a second machine over UDP. First manual flight at the MAVProxy prompt (arm / takeoff / RTL).
- **Topology learned:** commands go *up*, telemetry comes *down* — already the real drone's topology. The ground station is a *window*, not the brain: closing QGC mid-flight doesn't stop the drone.
- **`mission_basic.py` written:** connect → `GUIDED` → arm → takeoff → 5 m square → land, all in closed loop (each move confirmed by reading telemetry, never `sleep()`).
- **NED trap:** Down is positive toward the ground, so altitude is *negative* — fly at 10 m means `z = -10`. Standard aero convention, not an ArduPilot quirk.
- **Pre-arm checks:** arming is refused until the EKF has converged (~30 s after boot). Same check that protects the real drone — read the message, don't retry blindly.
