# EKF3 Optical-Flow Fusion — GPS-Denied Navigation Architecture

*Design document, started 2026-07-23. Status: architecture frozen, hardware on order, SITL validation next.*

## 1. Goal

Hover and loiter **without GPS**: EKF3 fuses body-frame optical flow (horizontal velocity)
with a downward ToF rangefinder (height above ground, i.e. the scale of the flow) on the
ARGOS 3.5" quad. The differentiator: the flow is **computed from a drone camera on a
companion computer** and injected into EKF3 over MAVLink — the same fusion pipeline a
commercial flow sensor would use, with our own visual velocity estimator as the source.

GPS stays onboard as a **measuring instrument only**: logged as ground truth for drift
benchmarks, never fused in the benchmark configuration.

## 2. The constraint that shaped the architecture

The SpeedyBee F405 Mini has exactly **one free full-duplex UART**:

| UART  | Use                              | Free?             |
|-------|----------------------------------|-------------------|
| UART1 | VTX (IRC Tramp)                  | no                |
| UART2 | RC in (ELRS/CRSF)                | no                |
| UART3 | —                                | **the only one**  |
| UART4 | internal Bluetooth (no pads)     | no                |
| UART5 | ESC telemetry (RX-only pad)      | no                |
| UART6 | GPS (u-blox M10)                 | no                |

The I2C bus (DA/CL pads) carries only the dead QMC5883 compass → effectively free.

Consequences:

- Whatever sits on UART3 must earn it. A **companion computer** there does triple duty:
  computes the flow, injects it as MAVLink, and bridges telemetry over WiFi (replacing the
  planned ESP32 DroneBridge — the Pi has WiFi).
- The ToF must be **I2C**, not UART.
- A UART flow sensor (MTF-02P) would *displace* the companion link, i.e. compete with the
  very brick it was supposed to bootstrap.

## 3. Sensor decisions (2026-07-23)

### ToF rangefinder: Benewake TFmini-S, I2C mode

Non-negotiable component — any flow source needs height-above-ground for scale.

| Candidate      | Interface           | Range (sun)   | Verdict |
|----------------|---------------------|---------------|---------|
| **TFmini-S**   | UART **+ I2C**      | 12 m (good)   | ✅ chosen: I2C = zero UART cost, driver already in the minimized build (`AP_RANGEFINDER_BENEWAKE_TFMINIPLUS_ENABLED 1`), outdoor-capable |
| TF-Luna        | UART only (in AP)   | 8 m (fair)    | ✗ ArduPilot supports it serial-only (type 27) → would eat UART3 |
| VL53L1X        | I2C                 | 4 m (~1 m ☀)  | ✗ dies in sunlight — indoor-only, wrong signal for this project |
| MTF-02P (ToF)  | UART (combo)        | 6 m (4 m ☀)   | ✗ see below |

Config: `RNGFND1_TYPE=25` (TFminiPlus-I2C), `RNGFND1_ADDR=16`, `RNGFND1_ORIENT=25` (down).
Caveat: ships in UART mode → one-time protocol-switch command over a USB-TTL adapter.
It shares the I2C bus with the (dead) compass at a different address (0x10 vs 0x0D) — and
leaves room for a future compass breakout on the same pads.

### MTF-02P: skipped (decision record)

Position (Victor, 2026-07-22): cheap indoor-bootstrap sensor, not what this project should
showcase. Analysis agrees for *structural* reasons, not just taste:

1. **UART conflict**: it needs UART3 — the companion's port. Wiring it delays the actual
   differentiator instead of de-risking it.
2. Its de-risk value (validate EKF3 params on a known-good flow source) is replaced at zero
   cost by the **SITL validation ladder** (§7): ArduPilot SITL simulates a flow sensor, and
   the injection path can be rehearsed end-to-end in software.
3. The rigorous benchmark number is **drift vs GPS ground truth**, which needs no second
   flow sensor.

**Tripwires to revisit** (any one → order it, ~€35, wire it to a *Pi UART*, not the FC, for
A/B flights with zero FC rewiring):
- camera-flow quality unusable after the bench wiggle-test tuning;
- measured injection latency > 250 ms (`EK3_FLOW_DELAY` ceiling);
- a head-to-head "my flow vs commercial flow" table becomes worth the effort.

### Companion + camera: Raspberry Pi Zero 2 W + global-shutter down-cam

- **Pi Zero 2 W** (~11 g): quad-core A53 — enough for sparse LK flow at 30–50 Hz on a
  downscaled mono image. Sits on UART3 (MAVLink2). WiFi = telemetry bridge (mavlink-router),
  absorbing the ESP32 DroneBridge role.
- **Arducam OV9281** (~3 g): 1 MP **global shutter**, mono — the correct flow camera
  (rolling shutter + frame vibration = flow artifacts; every serious flow sensor is global
  shutter). Mounted **facing down**.
- The FPV Phoenix2 stays exactly as-is (piloting + OSD + Mode A HUD). It **cannot** be the
  flow camera: it looks forward, and EKF3's flow model assumes a nadir sensor.
- Power: dedicated 5 V/2 A mini-BEC from the LiPo — the FC's 4V5/5V rails are already at
  budget (RX + GPS + VTX + cam).

## 4. Data flow

```
                            ┌────────────── drone ──────────────┐
 down cam (OV9281, global shutter)                              │
        │ CSI                                                   │
        ▼                                                       │
 Pi Zero 2 W ── LK flow @ 30–50 Hz ── px/s → rad/s ── quality   │
        │                                                       │
        │ MAVLink2 @ 921600, UART3            TFmini-S (I2C)    │
        ▼                                          │            │
   OPTICAL_FLOW (flow_rate_x/y [rad/s], quality)   ▼            │
        └──────────► AP_OpticalFlow_MAV ──► EKF3 ◄── RNGFND (AGL, terrain est.)
                                             │
                              velocity / position states
                                             │
                              FlowHold · Loiter (flow-only)
```

Notes anchored in the source (`AP_OpticalFlow_MAV.cpp`):
- The MAV backend **prefers `flow_rate_x/flow_rate_y`** (rad/s, float) over the legacy
  integer `flow_x/flow_y` — send the rate fields, leave the legacy ones at 0.
- Messages are averaged between EKF pulls; quality is the mean of received `quality` (0–255).
  Gate at the source: don't send when tracked-feature count is poor — quality drives fusion.
- Timestamping is at receipt ("ToDo: add jitter correction" in the driver) → keep the send
  cadence steady; latency is handled by `EK3_FLOW_DELAY`, jitter is not.

## 5. FC-side configuration

Firmware (done 2026-07-23, branch `argos-custom`): `AP_OPTICALFLOW_ENABLED 1` restored in
the SpeedyBeeF405Mini hwdef (the 1 MB minimize include forces it off), **MAVLink backend
only** (hardware-flow drivers stay out), FlowHold mode restored. EKF3 flow fusion + AGL
estimator follow the flag automatically (`AP_NavEKF3_feature.h`). Cost: fits with ~126 KB
flash free. hwdef gotcha learned: overriding an include's `define` requires `undef` first,
and hwdef.h is only regenerated by `waf configure`.

Parameters (to set at bring-up):

| Param | Value | Why |
|---|---|---|
| `SERIAL3_PROTOCOL` | 2 | MAVLink2 to the Pi |
| `SERIAL3_BAUD` | 921 | USART3 has DMA; headroom for telemetry + flow |
| `FLOW_TYPE` | 5 | MAVLink flow backend |
| `FLOW_ORIENT_YAW` | per mounting | camera yaw vs airframe, centidegrees |
| `FLOW_FXSCALER/FYSCALER` | 0 → calibrated | in-flight flow calibrator kept in the build |
| `RNGFND1_TYPE` | 25 | TFminiPlus-I2C driver (TFmini-S in I2C mode) |
| `RNGFND1_ADDR` | 16 | 0x10 default |
| `RNGFND1_ORIENT` | 25 | down |
| `RNGFND1_MIN/MAX` | 0.1 / 8 m | conservative outdoor max |
| `EK3_SRC1_POSXY` | 0 | no position source — GPS-denied |
| `EK3_SRC1_VELXY` | 5 | optical flow |
| `EK3_SRC1_POSZ` | 1 | baro for the height state; rangefinder feeds the terrain estimator (leave `EK3_RNG_USE_HGT=-1`) |
| `EK3_SRC1_YAW` | 0 (no compass) | see below |
| `EK3_SRC2_*` | GPS set | outdoor safety net via source-select switch (`RCx_OPTION=90`) — never used during benchmark runs |
| `EK3_FLOW_USE` | 1 | fuse for navigation |
| `EK3_FLOW_DELAY` | measured (§6) | 0–250 ms |
| `FS_EKF_ACTION` | 1 (Land) | flow degrades → land, not RTL (no GPS nav) |

**Yaw without a compass** — verified in `AP_NavEKF3_Control.cpp` (`setAidingMode`): flow
fusion is *AID_RELATIVE*, and the code comments explicitly that body-frame sensors
(optical flow, body odometry) **do not require yaw alignment**. Flow-only flight with
`EK3_SRC1_YAW=0` (gyro-integrated heading, slow drift) is a supported configuration.
Optional later: a €5 QMC5883L (or better RM3100) breakout on the same DA/CL pads revives
the dead-compass plan B and gives absolute heading for outdoor GPS modes.

## 6. Latency: budget and measurement

Budget (onboard path): exposure+capture ~10–20 ms, flow compute ~10–20 ms, UART ~2 ms →
**~30–45 ms**, comfortably inside `EK3_FLOW_DELAY`'s 0–250 ms range. (This is the reason
the ground-loop variant — analog video down, flow on the laptop, WiFi back up — was demoted
to an experiment: it adds the whole radio chain to the loop and its jitter is uncorrected.)

Measurement (also validates axis signs — the classic flow failure is an inverted axis →
instant divergence): hand-carry the armed-in-bench drone over a textured floor, roll/pitch
wiggles, then cross-correlate logged `OF.flowX/Y` against IMU gyro X/Y. The lag of the
correlation peak **is** `EK3_FLOW_DELAY`; the sign of the peak checks conventions. Same
ritual as the log analyses of 2026-07-23 — download DataFlash, pymavlink from WSL.

## 7. Validation ladder (SITL-first, then hardware)

1. **SITL native flow** (simulated flow + rangefinder): validate the whole `EK3_SRC*` set,
   FlowHold and flow-only Loiter behavior, EKF failsafe, source-select switch. No hardware.
2. **SITL + injector**: a Python script feeds `OPTICAL_FLOW` into SITL (`FLOW_TYPE=5`) —
   rehearses the exact injection path, delay compensation, quality gating.
3. **Gazebo rehearsal**: Gazebo down-camera → *the real flow algorithm* → SITL EKF3. Full
   software dress rehearsal of the differentiator on the existing Gazebo rig (`gz_camera.py`).
4. **Bench**: wiggle test (§6) → signs + delay + quality sanity in the logs.
5. **Flight**: FlowHold (no rangefinder needed — testable before the TFmini-S arrives) →
   flow-only Loiter over grass → benchmark runs.

## 8. Benchmark (the publishable number)

- **Metric**: horizontal drift of the EKF3 flow-only position vs logged GPS ground truth,
  during 60 s hovers in flow-only Loiter; report p50/p95 over ≥10 runs, plus flow quality
  and EKF innovation stats.
- **Conditions matrix**: grass vs low-texture ground; 1 m / 2 m / 5 m AGL; light levels.
- GPS is *logged, never fused* in benchmark runs (`EK3_SRC1` active); a source-select
  switch arms the GPS fallback for safety between runs.

## 9. Shopping list (2026-07-23)

| Item | Purpose | ~€ |
|---|---|---|
| Benewake TFmini-S | ToF, I2C on DA/CL | 40 |
| Raspberry Pi Zero 2 W + 32 GB SD | companion: flow + telemetry bridge | 25 |
| Arducam OV9281 global shutter + 22-pin FFC (Zero format) | down-facing flow camera | 30 |
| 5 V/2–3 A mini-BEC (e.g. Matek) | Pi power from LiPo | 6 |
| USB-TTL 3.3 V adapter | TFmini-S UART→I2C mode switch, serial debug | 5 |
| *(optional)* QMC5883L GY-271 breakout | compass plan B on DA/CL | 4 |
