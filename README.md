# ARGOS

> A miniature, end-to-end ISR drone system: **GPS-denied indoor flight**, **real-time object detection on the edge**, and a **closed detection → guidance loop** that steers the drone toward what it sees. Built simulation-first, then on real hardware — with measured numbers at every stage.

**Status:** Week 1 / 6 — SITL phase (ArduPilot software-in-the-loop). Build-in-public, iterating in the open.

---

## What it is

ARGOS is a compact ISR (Intelligence, Surveillance, Reconnaissance) pipeline built from the ground up:

- a drone running **ArduPilot** (compiled from source) that holds position **indoors, without GPS**, using an optical-flow + time-of-flight sensor fused by the **EKF3** state estimator;
- an analog **5.8 GHz** video downlink to a ground station;
- an **INT8-quantized object detector** (TensorRT) running in real time on that ground station;
- a feedback loop that turns a visual detection into **MAVLink guidance commands** to *track & follow* a locked target.

Two modes capture the idea: **Mode A** — the ground station shows what the drone sees (real-time detection overlay). **Mode B** — the ground station takes the stick (closed-loop yaw tracking).

## The three primitives

| ARGOS block | Real ISR problem it addresses |
|---|---|
| GPS-denied hover (optical flow + ToF + EKF3) | In contested areas GNSS is jammed — you must navigate GPS-denied |
| Real-time INT8 detection on edge hardware | Video analysis must run on-site, offline, on constrained compute |
| Detection → MAVLink guidance loop | Turn a visual detection into a flight command toward the target |

## Architecture

```
   ┌── GROUND STATION (RTX 4070 → Jetson Orin Nano) ─┐
   │ capture → decode → INT8 detector → tracker      │
   │ → yaw control law                               │
   │ Mode A: HUD overlay      Mode B: MAVSDK (C++)   │
   └────▲────────────────────────────┬───────────────┘
   5.8GHz analog video          MAVLink over WiFi
   (cam → VTX → RX →            (ESP32-C3 + DroneBridge
    AV/HDMI → UVC capture)       ↔ FC TELEM UART)
   ┌────┴────────────────────────────▼───────────────┐
   │ Flight controller (F405) running ArduCopter      │
   │ EKF3 · FlowHold/Loiter · optical-flow rangefinder│
   │ ELRS RX ← handheld radio = human failsafe        │
   └──────────────────────────────────────────────────┘
```

## Roadmap

- **S1 — SITL** *(current)*: ArduPilot SITL + MAVProxy + QGroundControl; first scripted mission in `GUIDED` via pymavlink.
- **S2 — C++ & perception**: MAVSDK guidance app (continuous yaw-rate control); YOLO → ONNX → TensorRT (FP32/FP16/INT8) with a published mAP / FPS / latency benchmark.
- **S3 — Hardware bench**: compile & flash ArduPilot to the FC; wire the flow sensor; wireless MAVLink telemetry; sensors live in QGC; motors armed (props off) driven from the app.
- **S4 — Build & Mode A**: 3.5" build; first manual flights; real-time detection HUD overlay; indoor FlowHold hover with measured drift.
- **S5 — Mode B in flight**: closed detection → yaw → MAVLink loop; end-to-end latency p50/p95, command rate, loss-of-target behavior.
- **S6 — Polish**: final README with measurement tables, videos, known limits.

Every milestone ships a **published number** (mAP, FPS, latency p50/p95, position drift, perf/W).

## Repository layout

```
argos/
├── sitl/              # SITL-phase scripts (pymavlink)
│   └── mission_basic.py   # arm → takeoff → square → land, in GUIDED
├── docs/
│   └── journal.md     # dev journal: discoveries, surprises, traps
├── README.md
└── LICENSE
```

## Tech stack

ArduPilot · MAVLink · SITL · MAVProxy · QGroundControl · pymavlink · MAVSDK (C++) · EKF3 · OpenCV · YOLO (Ultralytics) · ONNX · TensorRT · INT8 quantization · Jetson / JetPack · ESP32 DroneBridge · ELRS/CRSF

## Scope & ethics

This is a **track-and-follow** visual-pursuit demonstrator, not a targeting system. All demos are **indoor**; every closed-loop test is run **props-off first**, with a hardware **kill switch** configured and a human pilot holding the radio as a permanent failsafe.

## License

MIT — see [LICENSE](LICENSE).
