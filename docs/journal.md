# Dev journal

One line per discovery, surprise, or trap. Raw and chronological — this is the
memory cement: interview material, build-in-public content, and my own notes.

---

## 📍 ÉTAT COURANT — mis à jour 2026-08-02

> Bloc réécrit à chaque session. Répond à « j'en suis où ? », pas à « que s'est-il passé ? ».
> L'historique est plus bas, il ne bouge jamais.

**Où j'en suis.** Le drone 3,5" **vole stable** (résonance résolue le 29/07 : cycle limite
entretenu par la boucle, `ATC_RAT_RLL/PIT_P = I = 0,06`, `D = 0,0005` → 0 % de temps en
résonance). Config validée versionnée dans `config/argos-drone-2026-07-29.param`. Chapitre
hardware **clos** pour l'instant.

**Prochaine action concrète.** `GUIDED_NOGPS` en SITL + Gazebo : loi d'attitude à partir de
l'erreur pixel, garde sur la taille de bbox, cap relatif. 100 % logiciel, aucune dépendance
matérielle. Détail dans `PORTFOLIO.md` §1.1 et §1.4.

**Périmètre de la fin de l'ARGOS mono-drone** (arrêté le 02/08) : drone pilotable à la main →
la station sol reçoit la vidéo + HUD → l'opérateur déclenche le hover autonome → il lock une
cible → le drone l'engage en terminal guidance. En SITL, puis HITL, puis sur le 3,5", puis
éventuellement sur le whoop. Le tout avec un dialecte MAVLink maison.

**Décisions actées.**
- **Tout le calcul se fait au sol**, sur le fixe (RTX 4060). Pas de calculateur embarqué,
  pas d'achat. La liaison de commande passera par ESP32-C3 / DroneBridge.
- **Aucun achat** sur ce projet. Conséquence corrigée le 02/08 au soir : **le hover en flow est
  matériellement impossible sur le 3,5"** — EKF3 exige une caméra au nadir, la Phoenix2 regarde
  devant, et il n'y a ni companion ni caméra nadir. Donc hover en flow **en simu uniquement** ;
  sur le vrai drone, soit hover **au GPS** (le M10 marche, à recâbler en série seule sans DA/CL),
  soit pas de hover autonome du tout. **Décision ouverte.**
- **Ordre imposé : engagement d'abord, hover autonome ensuite.** Le hover est le morceau où
  les projets s'enlisent ; il ne doit pas bloquer l'artefact montrable.

**Blocages / points ouverts.**
- ⚠ Le hwdef `SpeedyBeeF405Mini` du fork ArduPilot (`argos-custom`) est **stagé mais pas
  commité**, et il n'est pas certain que la FC tourne un build contenant le mode 20
  (`GUIDED_NOGPS`). À vérifier avant tout vol qui en dépend.
- Mousse cellules ouvertes sur le baro DPS310 : prérequis avant de compter sur l'altitude
  tenue par `thrust = 0.5`.
- Le mode mécanique à 15,5 Hz existe toujours, on est sous le seuil d'excitation mais pas
  loin → pas d'Autotune tel quel.

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

## 2026-06-19 (suite) — Camera-in-the-loop : POV Gazebo + détection + tracking GIMBAL

Branché la **vraie caméra du drone Gazebo** dans la console opérateur (POV live + détection +
HUD + suivi de cible), ouvrable depuis le Mac via Tailscale. Pipeline de bout en bout validé.

**Ce qui marche (validé end-to-end) :**
- **Ingestion caméra** : `perception/gz_camera.py` lit le topic image gz via les bindings
  Python `gz-transport13`/`gz-msgs10` (apt). Frame brute RGB_INT8 640×480 → BGR numpy. Un `.pth`
  dans `perception/.venv` expose `/usr/lib/python3/dist-packages` (le venv garde la priorité).
- **Scène** : monde ARGOS `sitl/gazebo/worlds/argos_demo.sdf` (copie d'iris_runway) + 2 cibles
  Fuel (Hatchback + Standing person) devant le drone. `run_gazebo.sh` pointe dessus.
- **Caméra ISR** : gimbal pitché ~-0.8 rad (vise le sol devant) + **crop du haut 50%** (retire
  l'airframe, qui sinon se fait détecter comme "airplane"). Donne person ~0.72 / car ~0.67.
- **Détection** : COCO `yolo11n.pt` quand source=gazebo (remap person→personne,
  car/truck/bus/moto→vehicule), réutilise `detect()`/`draw_boxes()`. Source "gazebo" ajoutée au
  menu à côté des 3 vidéos réelles (Mode A inchangé).
- **Tracking par GIMBAL (boucle fermée)** : l'opérateur lock une détection → l'erreur (cible vs
  centre image) intègre l'angle de **yaw du gimbal** → le pod slew → la cible se recentre.
  Converge proprement : err +0.13 → 0.00 et tient, gimbal_yaw stable. C'est un vrai pod ISR.

**Limites physiques découvertes (importantes) :** l'iris Gazebo ne peut **PAS** :
- **yawer** (pas de couple de lacet modélisé — confirmé : CONDITION_YAW rejeté ACK 4, yaw_rate
  ignoré, même RC override en ALT_HOLD ne tourne pas) ;
- **se déplacer horizontalement** via les setpoints GUIDED (`SET_POSITION_TARGET` vélocité ET
  position ET `DO_REPOSITION` : tous ignorés ; seuls arm + NAV_TAKEOFF marchent).
→ D'où le **pivot du tracking vers le gimbal** (au lieu de yawer le drone comme le hack viewport
des sources vidéo). Plus réaliste de toute façon. **ENGAGE** (le drone avance vers la cible)
est câblé (vitesse NED vers le relèvement gimbal) mais **best-effort** : bloqué par la limite
setpoint ci-dessus. À creuser (piste : la limite vient peut-être du sous-mode takeoff GUIDED ou
d'un réglage SITL+Gazebo).

**Autres correctifs :**
- `lock_step=0` (no_lockstep) dans `models/iris_with_gimbal/model.sdf` : évite le deadlock
  SITL↔Gazebo sous mavproxy (sim_time gelait). N'affecte pas les manœuvres (le blocage yaw/vel
  est indépendant du lockstep).
- **Canaux gimbal 8/9/10 retirés** de l'ArduPilotPlugin (`iris_with_gimbal/model.sdf`) :
  ArduPilot publiait sur `/gimbal/cmd_*` et écrasait nos commandes une fois en vol → le gimbal
  est maintenant piloté exclusivement par la console (JointPositionController).
- Gimbal commandé via le **CLI `gz topic`** (subprocess) depuis un thread dédié `_gimbal_thread`
  (~8 Hz) : le publish gz-transport python ne porte pas hors du thread principal.
- `console.py` : `DRONE_CONN` défaut `udp:127.0.0.1:14551` (sortie mavproxy) ; `_drone_thread`
  attend un fix GPS 3D avant d'armer (décollage fiable) ; `TAKEOFF_ALT=12` (cadre les cibles).
- `sitl/gazebo_takeoff_test.py` : atterrit + désarme à la fin (ne reste plus en l'air).

**Lancer la démo :** `./sitl/run_gazebo.sh` puis `make console` (ou
`perception/.venv/bin/python perception/console.py`) → `http://<fixe-tailscale>:8088`, source
"POV drone · Gazebo", Décoller, cliquer une cible → le gimbal la suit.

## 2026-06-19 (3) — ENGAGE débloqué : corrections majeures sur la physique & le gimbal

Reprise sur une simu FRAÎCHE (Victor a reset le PC). Plusieurs conclusions d'avant étaient des
**artefacts de simu dégradée** (mes tests crashaient le drone en boucle). Vérité établie par des
tests directs MAVLink sur la simu propre :

- **Le drone PEUT translater** ✅ : un setpoint de vitesse NED le fait bouger (pitch -17°, vol à
  4 m/s, position qui change). L'ancien « setpoints ignorés » était faux. → **ENGAGE est faisable.**
- **Le drone ne peut TOUJOURS pas yawer** ❌ (confirmé : yaw_rate + CONDITION_YAW + RC override yaw
  tous sans effet) — pas de couple de lacet dans le modèle iris.
- **Le gimbal se pilote via ArduPilot (mount RC), PAS via gz topic.** Mon retrait des canaux 8/9/10
  avait cassé l'actionnement du gimbal (c'est le canal ArduPilot qui applique la force au joint, pas
  le JointPositionController seul). **Restauré les canaux** + ajouté le param officiel
  `config/gazebo-iris-gimbal.parm` (MNT1_TYPE=1, SERVO9-11_FUNCTION, RC7_OPTION=213 pitch,
  RC8_OPTION=214 yaw, MNT1_DEFLT_MODE=3 RC_TARGETING) au lancement (`run_gazebo.sh`).
  → **Gimbal piloté en RC override : RC7=pitch (1500≈nadir, ~1610=avant-bas), RC8=yaw.** Validé
  (le pitch et le yaw bougent la caméra).

**Nouvelle architecture du suivi (console)** — comme le drone translate mais ne yaw pas :
- gimbal **fixe** (RC7 pitch avant-bas, RC8 neutre) tenu en continu par `_drone_thread` (RC override
  renvoyé à 5 Hz, l'override expire en ~3 s) ;
- **suivi par TRANSLATION** : l'erreur horizontale → strafe Est/Ouest du drone pour recentrer la
  cible ; **ENGAGE** = vitesse avant. Tout en vitesse NED (validé). Remplacé l'ancien tracking
  gz-topic gimbal (cassé). Constantes à régler : `RC7_PITCH`, `K_STRAFE`, `STRAFE_SIGN`,
  `ENGAGE_SPEED`, `GZ_IMGSZ`.

**Point dur restant = la DÉTECTION synthétique** (domain gap COCO) : la voiture Hatchback est mal
classée depuis l'aérien (kite/airplane) ; la personne ne sort bien qu'en `imgsz=1280` (0.68). Et
l'angle exact du gimbal est très sensible (vue qui varie). → À **régler en live** (voir la POV +
les détections en direct est ~10× plus rapide que mes cycles de vol aveugles de 3 min). Pistes :
altitude plus basse / cible plus proche (vue oblique, plus gros), autre modèle de véhicule,
ajustement de `RC7_PITCH`.

## 2026-06-19 (4) — Camera-in-the-loop : DÉMO QUI MARCHE (suivi opérateur d'une personne)

Validé en live dans la console (navigateur). Mise à jour des conclusions précédentes qui
étaient pessimistes (ENGAGE n'est PAS bloqué — il marche).

**Ce qui marche, de bout en bout :** POV réelle du drone Gazebo → détection COCO → l'opérateur
clique pour **locker** une personne → le drone **strafe pour la centrer** → **ENGAGE** : il
avance vers elle et **se maintient** (sans la dépasser). Cap tenu, vrai firmware ArduPilot,
physique Gazebo. + **pilotage manuel** (boutons Monter/Avancer/… pour positionner le drone).

**Les surprises de l'iris Gazebo (toutes contournées) :**
- **Ne peut pas yawer** (pas de couple de lacet) → on ne tourne pas le drone.
- **Tourne le nez vers sa vitesse** par défaut (`WP_YAW_BEHAVIOR`=1) → chaque déplacement
  pointait la caméra hors cible. **Fix : `WP_YAW_BEHAVIOR=0`** (cap fixe). *Indispensable.*
- **PEUT translater** (vitesse NED OK — l'ancien « setpoints ignorés » était un artefact de
  simu crashée). → **suivi par TRANSLATION** : l'erreur image horizontale → strafe Est/Ouest
  pour recentrer ; ENGAGE = vitesse avant + **stop-when-close** (cible basse dans l'image =
  proche → on cesse d'avancer, on se maintient).
- **Gimbal = mount ArduPilot, piloté en RC override** (PAS gz topic). Param officiel
  `config/gazebo-iris-gimbal.parm` (MNT1, RC6/7/8_OPTION, SERVO9-11_FUNCTION, MNT1_DEFLT_MODE=3)
  ajouté au lancement. **RC6=roll (à plat), RC7=pitch (~1610 avant-bas), RC8=yaw.** Tenu à 5 Hz
  (l'override expire en ~3 s). NB : retirer les canaux 8/9/10 du plugin CASSE l'actionnement du
  gimbal — il faut les garder.

**Détection synthétique (le point dur) :** COCO sur rendus Gazebo. La personne ne sortait pas
(trop petite avec le FOV 114°). **Fix : réduire le FOV caméra** (`gimbal_small_3d` horizontal_fov
2.0 → 1.2 rad ≈ 69°) → cibles ~1.7× plus grosses, personne fiable (~0.6-0.7). + `imgsz=1280`,
crop du haut, et un **COAST** (on garde le lock ~2 s après une perte → robuste au flicker ~30 %).
La **voiture (Hatchback) reste mal détectée** depuis l'aérien (lue comme kite/airplane) → démo
centrée personne ; swap de modèle véhicule à tenter plus tard.

**Réglages live (sans redémarrer la console) :** `/gimbal?pitch=&yaw=`, `/tune?kstrafe=&gate=&coast=`,
`/fly?vN=&vE=&vD=&dur=`. **Lancer :** `./sitl/run_gazebo.sh` (T1) + `python perception/console.py`
(T2) → `http://<fixe-tailscale>:8088`, source "POV drone · Gazebo". **Toujours redémarrer la
console quand on redémarre la simu** (sinon connexion drone périmée).

## 2026-07-01 — Analyse stack Alta Ares → 2 nouvelles briques planifiées

Victor a ajouté `stack-tech-altaares.md` (3 annonces Alta Ares : MAVLink & Autopilot Engineer,
Embedded SWE, spontanée). Analyse croisée avec l'état d'ARGOS. Verdict : déjà très aligné
(SITL+Gazebo+MAVLink, companion computer, console opérateur, GNSS-denied ↔ leur C-UAS, log
DataFlash). Deux actions décidées, **planifiées mais pas encore codées** (voir `argos-plan-sprint.md` §8) :

- **A — MAVLink en profondeur (`ARGOS_TARGET`).** Leur compétence la plus martelée (poste dédié) :
  *messages/dialectes custom, parsers/routers, en C++ ET Python*. On va définir un dialecte XML
  custom + un message `ARGOS_TARGET`, le générer via `mavgen` en Python **et** C++, faire publier
  la perception dessus et le consommer côté guidance C++. Meilleur mapping 1:1 avec une fiche ouverte.
- **B — Démo ArduPlane SITL.** Leur fiche exige fixed-wing ET multi-rotor (« required »). ARGOS est
  copter-only = seule case vide. La démo ArduPlane (jusqu'ici ligne de coupe n°1) est **promue
  livrable**.

Aucun code touché aujourd'hui : session de planif. Mémoire interne mise à jour
(`reference-altaares-stack`). À attaquer en session dédiée quand Victor veut.

## 2026-07-15 — S3 kickoff : firmware compilé from source pour la SpeedyBee F405 Mini

**Le hardware est arrivé** : SpeedyBee F405 Mini (stack 20×20) + ESC BLS 35A Mini V2 4-en-1
(BLHeli_S, intégré au stack) + récepteur SpeedyBee Nano ELRS 2.4G/915. Début du bench S3.

**Firmware compilé from source (WSL)** — la toolchain était déjà en place (arm-none-eabi-gcc
10.2.1 = pile la version recommandée, `empy` 3.3.4 dans `~/venv-ardupilot`) :
`./waf configure --board SpeedyBeeF405Mini && ./waf copter` → 857 Ko de flash utilisés sur 1 Mo.
La cible `SpeedyBeeF405Mini` (hwdef vérifié) mappe déjà USART2 en RCIN → l'ELRS ira sur les
pads RX2, protocole CRSF auto-détecté. Fichiers produits dans
`ardupilot/build/SpeedyBeeF405Mini/bin/`, copiés côté Windows
(`C:\Users\victo\Desktop\ARGOS_firmware\`) :
- `arducopter_with_bl.hex` — bootloader ArduPilot + firmware, pour le **premier** flash en DFU
  (la carte sort d'usine sous Betaflight, son bootloader doit être remplacé) ;
- `arducopter.apj` — firmware seul, pour les mises à jour suivantes via le bootloader ArduPilot.

**FLASH RÉUSSI ✅ (confirmé le soir même).** Parcours (reconstitué depuis la session Windows de
Victor) : DFU + « no response from board » (premier essai raté), puis « Load custom firmware »
avec le `_with_bl.hex` → « upload via DFU » → `Found board type 1135 blrev 5 … STM32F40x` (le
bootloader ArduPilot répond) → après reboot, Windows énumère **« ArduPilot (COM3) »**. Premiers
connect à 115200 en échec (« La séquence ne contient aucun élément » = erreur MP après
changement de port — fermer/relancer MP), puis **connexion OK à 57600** : HUD qui suit les
mouvements de la carte, `ArduCopter V4.8.0-dev` annoncé, sélection de frame accessible.

**Vérif du hash — fausse alerte, puis confirmation : c'est BIEN le build from-source qui
tourne.** Une première lecture donnait `(996a50e9)` (un commit upstream absent du clone local),
d'où soupçon que MP avait flashé le firmware officiel par-dessus. Contre-vérification en double :
(1) MP refuse l'upload du `.apj` maison — « No need to upload. already on the board » (il compare
le hash du `.apj` à celui de la carte) ; (2) l'onglet **Messages** après connexion affiche
`ArduCopter V4.8.0-dev (740cbb71)` = exactement le `GIT_VERSION` du build local
(`build/SpeedyBeeF405Mini/ap_version.h`). Le `996a50e9` venait probablement de l'écran Install
Firmware (version officielle *téléchargeable*, pas installée). **Leçon retenue : la source de
vérité pour « quel firmware tourne » = l'onglet Messages après connexion, hash compris.**

**Acquis du jour** : premier flash = toujours `_with_bl.hex` en DFU (remplace le bootloader
Betaflight) ; ensuite le `.apj` suffit via le bootloader ArduPilot, sans toucher BOOT. Le baud
USB (57600 vs 115200) est anecdotique (CDC natif).

**Milestone : la SpeedyBee F405 Mini boote ArduCopter V4.8.0-dev compilé from source (740cbb71),
MAVLink OK, HUD réactif.** Prochaines étapes S3 (une à la fois, vérifiée) : frame Quad X +
calibration accéléro dans Mission Planner → bind ELRS + mapping RC + kill switch → motor test
ESC **sans hélices** → MTF-02P optical flow + EKF3.

## 2026-07-16 — Victor refait TOUTE la chaîne firmware lui-même (+ fork + custom banner)

Suite au constat honnête « c'est Claude qui a compilé, je ne peux pas le raconter en entretien »,
Victor a refait l'intégralité du chemin **de ses mains**, avec cette fois une vraie modification
du firmware. Calibrations (frame Quad X + accéléro + level) faites dans Mission Planner au
préalable.

**Ce qu'il a fait lui-même (tout vérifié) :**
- **Fork GitHub** `victormonnot/ardupilot` ; remotes rebranchés proprement sur le clone existant
  (`origin` = son fork, `upstream` = l'officiel) — motivation : récupérer son ArduPilot custom
  depuis plusieurs machines (Mac + fixe), le fork est le point de rencontre.
- **Branche `argos-custom`** ; modif de `ArduCopter/version.h` →
  `THISFIRMWARE "ArduCopter-ARGOS V4.8.0-dev"` ; **commit `8927564c`** « ARGOS: custom firmware
  banner » (posé sur `740cbb71`) ; branche **poussée sur le fork**.
- **Build par lui** : `./waf configure --board SpeedyBeeF405Mini && ./waf copter` →
  `GIT_VERSION "8927564c"` embarqué (vérifié dans `ap_version.h` et dans le `.apj` copié sur le
  Bureau Windows). Upload du `.apj` via Mission Planner (bootloader ArduPilot, plus de DFU).
- **Attendu côté carte** : bannière `ArduCopter-ARGOS V4.8.0-dev (8927564c)` dans Messages
  (= la preuve par le hash que SA modif tourne).

**Concepts consolidés au passage** (sessions d'explication à la demande) : compilation vs
interprétation (analogie ONNX→TensorRT : source portable → binaire spécifique au hardware),
cross-compilation x86→ARM (`file` sur les deux binaires : ELF x86-64 pour la SITL vs ELF ARM
32-bit pour la FC — même source), waf configure/build (≈ cmake/make de S2), `build/` = atelier
jetable gitignoré, `.apj` = `.bin` + métadonnées (board id, githash, checksum — c'est ce que
MP compare pour dire « already on the board »), clone vs fork vs branche (local vs GitHub),
hash git = empreinte SHA calculée du contenu, stockée dans `.git/objects/`, embarquée dans le
firmware à la compilation = mécanisme de traçabilité.

**Story d'entretien acquise** : « le firmware de ma FC, je l'ai modifié, compilé from source
sur ma machine, flashé via le bootloader que j'ai moi-même installé, et je peux le prouver par
le hash git que la carte annonce. »

**Multi-machine** : le Mac clone les deux repos (`argos` + fork `ardupilot`) dans la même
arborescence `~/argos-project/` ; le Mac = sources/édition, le fixe = build/flash/simu (venvs
et toolchains restent locaux à chaque machine).

**Étape suivante** : câblage + bind du récepteur SpeedyBee Nano ELRS sur RX2/TX2 (pinout à
préparer avant de sortir le fer), calibration radio, kill switch.

## 2026-07-20 — S3 bench (1/2) : première soudure, RX ELRS opérationnel, chaîne radio complète

**Le reste du matériel est arrivé — build 3.5" cohérent, inventaire validé** : frame FlyFishRC
Volador VX3.5 O4 Pro (« O4 » = marketing pour le système DJI numérique ; l'analogique se monte
sans souci), moteurs T-Motor F1404 3800KV (3-4S), hélices Gemfan Hurricane 3525 tripales,
caméra RunCam Phoenix 2 SP V3 + VTX SpeedyBee TX800 5.8G (analogique), LiPo Dogcom 4S 850 mAh
150C, chargeur ISDT 608PD (entrée DC/USB-C PD → prévoir une source PD ≥65 W). Bonus non
planifié : **GPS HGLRC M100-5883** (M10 + compas QMC5883) — comble l'absence de compas de la
F405 Mini ; ira sur T6/R6 + SDA/SCL, `SERIAL6` déjà GPS par défaut. Les 2× **ESP32-C3
SuperMini** du tiroir = les futurs ponts télémétrie DroneBridge du plan. Le NRF24L01 ne sert
pas (ELRS + ESP32 couvrent tout).

**Lecture du hwdef `SpeedyBeeF405Mini` — la carte est devenue lisible.** Mapping UART complet :
UART1 = VTX DJI par défaut (réutilisable, on est en analogique), **UART2 = RCIN (pads T2/R2,
CRSF auto) ← le RX**, UART3 = libre (candidat ESP32 DroneBridge), UART4 = Bluetooth interne,
UART5 = télémétrie ESC, **UART6 = GPS (pads T6/R6) ← le futur M100**. Point critique repéré :
`HAL_FRAME_TYPE_DEFAULT = 12` (**Betaflight X**) — l'ESC 4-en-1 du stack est câblé dans l'ordre
moteurs Betaflight, à vérifier dans les params avant le motor test (un `FRAME_TYPE=1` posé par
l'écran frame de MP casserait le mapping).

**Première soudure de sa vie** (fer 80 W réglable, étain 0.8 mm étiqueté « étain pur » mais
fusion à 183 °C = du 63/37 au plomb mal étiqueté, flux gel KINGBO RMA-218, Kapton pour
maintenir). Entraînement sur chutes de fil silicone (étamage ×10, jonctions) avant le vrai
job. Leçons gravées : **l'étain fond sur les pièces chauffées, pas sur la panne** ; **la panne
doit rester étamée** — le film liquide EST le pont thermique (l'envie de monter à 400 °C
venait d'une panne sèche, pas d'un manque de watts) ; 340-350 °C pour l'électronique, la
vraie haute température se réserve aux gros pads de puissance ; pads traversants (RX, trou
métallisé, faciles) vs pads plats (FC). Multimètre ANENG SZ308 découvert sans pile (6F22 9V
à acheter) → vérif anti-court visuelle en plan B, acceptée après inspection zoom.

**RX SpeedyBee Nano ELRS 2.4G soudé et bindé.** Câblage croisé RX→FC : `5V→4V5`, `G→G`,
`T→R2`, `R→T2` (4 fils, full duplex = télémétrie CRSF vers la radio). Antenne U.FL clipsée
AVANT mise sous tension (règle RF : jamais d'émetteur sans antenne — vital pour le VTX plus
tard). Premier boot : double clignotement = bind mode auto (RX jamais bindé). Radio =
**RadioMaster Pocket, module ELRS interne 2.4G** — version lue via le script Lua ExpressLRS :
`LBT_3.3.1 CE` + hash de build `e051b8` (même mécanisme de traçabilité que le `8927564c`
d'ArduPilot — le concept se généralise à tout l'embarqué open source). Majeure 3.x des deux
côtés → bind direct via [Bind] du Lua (après un détour involontaire par le mode WiFi du RX —
il y bascule seul après ~60 s sans lien ; power cycle et c'est réglé). LED fixe = lien établi.

**Chaîne radio validée de bout en bout dans Mission Planner** : manches → ELRS → RX → soudures
→ UART2 → ArduPilot → barres MP. Calibration radio faite (992-2011), ordre AETR d'origine
EdgeTX correct, convention pitch inversé notée. Cartographie des commandes de la Pocket :
voie 5 = épaule gauche 2 pos, voie 6 = gauche 3 pos, voie 7 = droite 3 pos, voie 8 = extrême
droite 2 pos, voie 9 = dos 2 pos, voie 10 = molette. Victor a repéré seul que les voies 15/16
frémissent avec la distance = **LQ/RSSI injectés par ELRS** (jauges de lien, pas des commandes).

**Mapping contrôles + kill switch + failsafe — tout testé.** Params écrits : `RC5_OPTION=153`
(ArmDisarm, épaule gauche = mémoire musculaire tinywhoop), `FLTMODE_CH=6` (modes sur le 3 pos
gauche — obligatoire dès que l'arm prend la voie 5, sinon double emploi), `RC8_OPTION=31`
(**Motor Emergency Stop** sur le 2 pos de droite — côté opposé à l'arm, pas de confusion
possible). Leçon de philosophie : Betaflight coupe les moteurs au disarm (l'arm switch du
tinywhoop ÉTAIT un kill de fait) ; ArduPilot protège le disarm (refus en vol, checks) et
sépare le coupe-tout inconditionnel = option 31. **Kill switch prouvé par test croisé** :
kill actif + tentative d'arm → `Arm: Motors Emergency Stopped` dans Messages (cette version
ne loggue pas la bascule elle-même — la preuve fonctionnelle vaut mieux). **Failsafe radio
testé** : radio éteinte → FAILSAFE rouge au HUD + « Radio Failsafe » ; rallumée → « Radio
Failsafe Cleared », reprise auto. Comportement ELRS = *no pulses* (perte franche, exactement
ce qu'ArduPilot attend). Messages pré-arm actuels normaux sur USB : batterie low voltage
(pas de LiPo) + Compass1 not healthy (pas de compas avant le M100). À poser à la prep vol :
`FS_THR_ENABLE=3` (Land) pour l'indoor — RTL sans GPS impossible et dangereux sous plafond.

**Montage frame commencé.** Stack sur le pattern **20×20** (la frame offre aussi 25,5×25,5 —
suivre SON matériel, pas la vidéo de référence). Ordre : ESC en bas (câblage lourd), FC
au-dessus (**flèche vers l'avant**, USB accessible), nappe 8 broches entre les deux, sandwich
plots anti-vibration + entretoises + écrous en haut serrés doux. Vis moteur : bras 3,5 mm
d'épaisseur → ~2,5 mm d'engagement avec des vis de 6 mm ; jamais forcer une vis qui bute
(bobinage dessous). Leçon d'intégration (trouvée par Victor) : **la géométrie d'abord, les
longueurs de fil ensuite** — moteurs montés sur les bras avant de couper/souder leurs fils.
La **calibration accéléro sera refaite sur le drone assemblé** (celle de la carte nue était
bancale — et c'est de toute façon la bonne pratique : on calibre l'objet final, calibrate
level en posture d'atterrissage).

**Suite (2/2) dans la prochaine entrée** : 12 fils moteurs + XT60 + condensateur low-ESR sur
l'ESC (soudure de puissance, fer à 400+ °C légitime), gate sécurité pile 9V → continuité +/−
avant première LiPo, vérif `FRAME_TYPE=12`, passage DShot300, motor test SANS hélices dans MP.

## 2026-07-21 — S3 bench (2/2) : soudures de puissance, première LiPo, motor test 4/4 ✅

**Frame montée, stack intégré.** Volador VX3.5 assemblée (notice Scribd VX3/VX3.5 + guide
Oscar Liang + vidéo build en appui), moteurs vissés sur les bras (vis courtes — bras 3,5 mm,
jamais forcer une vis qui bute = bobinage dessous). Stack 20×20 : ESC en bas, FC au-dessus
flèche vers l'avant, nappe 8 broches (moteurs + tension/courant batterie), **pas d'entretoise
rigide entre les cartes** — les plots silicone SONT les entretoises, précontrainte légère à
l'écrou (appui doux pour engager, serrage en croix, jamais écraser : c'est l'isolation
anti-vibration du gyro).

**Soudures de puissance** (fer à 390-420 °C — la haute température légitime, celle qui
compense la masse thermique, pas les défauts de geste). 12 fils moteurs coupés à longueur
sur la frame (géométrie d'abord), chaque moteur sur SON coin d'ESC, ordre des 3 phases
indifférent. XT60 rouge→`+` triple-vérifié + condensateur low-ESR **470 µF 35 V Rubycon ZLH**
(le 1000 µF en rechange), pattes courtes, bande = patte négative, corps immobilisé. Leçon
majeure au passage : les premiers joints qui « pelaient » = **défaut de mouillage** (joint
froid) — le pad n'était pas assez chaud, l'étain perlait dessus au lieu de s'y étaler.
Corrigé par : goutte d'étain sur la panne comme pont thermique + flux + méplat pressé +
patience jusqu'à voir l'étain *couler*. Un joint de puissance réussi ne s'arrache pas.

**Multimètre apprivoisé** (ANENG SZ308 + pile 9V/6F22) : mode continuité — et une mesure
plus parlante que prévu : en mode Ω sur le XT60, lecture qui grimpe puis « 1 » (infini) =
**le condo qui se charge sous le courant de test** — signature d'un rail sain, pas de court.
Mode V⎓ : LiPo à 15,4 V = charge de stockage (~3,85 V/cellule), suffisante pour le bench.
Concept calibres manuels compris (2000m = 2 V max → saturation sur une 4S).

**Première LiPo : baptême réussi.** Étincelle de charge du condo (entendue en grésillement —
brancher franchement la prochaine fois), mélodie ESC jouée par les moteurs, aucune chauffe,
aucune odeur, tension remontée à la FC : **Bat1 15,17 V dans MP** (≈ multimètre → monitoring
batterie validé), message pré-arm batterie disparu.

**Le piège `FRAME_TYPE` s'est confirmé** : le param était à **1** (X classique) — écrasé par
l'écran frame de MP à la calibration du 15/07 — alors que l'ESC du stack est câblé en ordre
Betaflight. Remis à **12** (Betaflight X). Invisible au bench, retournement garanti au
décollage : exactement ce que le motor test sert à attraper. `MOT_PWM_TYPE=5` (DShot300,
numérique, pas de calibration de plage, BLHeli_S natif).

**Motor test (sans hélices, throttle 5-8 %, drapeaux de scotch sur les cloches)** :
- Mapping positions : **4/4 parfait** du premier coup — A=avant-droit, B=arrière-droit,
  C=arrière-gauche, D=avant-gauche (MP affiche le mapping BF : A→Motor2, B→Motor1…).
- Sens : les 4 inversés uniformément (câblage des phases cohérent → miroir global).
  `SERVO_BLH_RVMASK=15` sans effet (BLHeli_S 16.7 stock ignore la commande DShot
  d'inversion) → **corrigé via BLHeliSuite16 en passthrough** (`SERVO_BLH_AUTO=1`, MP fermé,
  LiPo branchée, interface « SILABS BLHeli Bootloader (C/F) », COM3) : les 4 ESC détectés
  (`J_H_40`, rev 16.7), **Motor Direction → Reversed** ×4, Write Setup. Re-test : **A CCW,
  B CW, C CCW, D CW — conforme 4/4**. Correction stockée DANS les ESC (survit aux reflash
  FC). `RVMASK` remis à 0 (éviter une double inversion si un futur firmware honore la
  commande).

**Recalibration accéléro sur le drone assemblé** (6 positions + Calibrate Level en posture
d'atterrissage) — remplace la calibration bancale faite sur carte nue le 15/07 ; on calibre
l'objet final, pas un composant.

**Bilan S3 bench : le drone existe.** Radio + kill switch + failsafe testés (1/2), propulsion
mappée et vérifiée dans les deux sens du terme (2/2), alimentation saine, monitoring batterie
opérationnel. Premières soudures de sa vie → un drone qui répond. Reste avant premier vol :
VTX + caméra (UART libre à choisir), GPS M100-5883 sur T6/R6 + I2C, `FS_THR_ENABLE=3` (Land)
et choix des modes sur la voie 6, charge complète LiPo (source USB-C PD pour l'ISDT), et
LE différenciateur : **MTF-02P optical flow + EKF3** = le chapitre GPS-denied.

## 2026-07-21 (soir) — S3 périphériques : VTX + caméra + GPS soudés, u-blox détecté ✅, compas en debug

**Corrections au plan, vérifiées à la source avant de souder.** (1) Le TX800 parle **IRC
Tramp, pas SmartAudio** (page SpeedyBee + test Oscar Liang) → `SERIAL1_PROTOCOL=44`. Et lu
dans le code du firmware : contrairement à SmartAudio qui active le half-duplex tout seul
(`AP_SmartAudio.cpp:57`), le driver Tramp ne le fait pas et sa machine à états attend des
réponses du VTX sur le fil unique → **`SERIAL1_OPTIONS=4` obligatoire**. Tramp est compilé
d'office dans le build 1 Mo (forcé par `minimize_fpv_osd.inc`). (2) Le TX800 s'alimente en
**5V (3,7–5,5 V, ≤750 mA)** — ses voisins de pads `BAT` et `9V` seraient mortels. (3) Le
manuel du stack (schéma p.6 lu image par image) route le fil IRC vers **T1** : UART1 est LE
pad VTX analogique prévu par SpeedyBee ; UART3 reste 100 % libre pour l'ESP32 DroneBridge.
(4) France : 5,8 GHz limité à 25 mW → `VTX_MAX_POWER=25`. Mine repérée pour plus tard :
`minimize_fpv_osd.inc` fait **`AP_OPTICALFLOW_ENABLED 0`** → à réactiver dans le build
custom pour le chapitre MTF-02P.

**Câblage (13 soudures).** Caméra Phoenix 2 : 3 fils seulement (rouge→`5V`, noir→`G`,
jaune→`CAM`) — les fils menu/OSD (pack bleu+noir + fil seul) isolés, le pad `CC` Betaflight
ne sert à rien sous ArduPilot. VTX : `5V/G/VTX/T1` via le pigtail JST 4 broches. GPS
M100-5883 : rouge→`4V5` (rail vivant sur USB → bench sans LiPo), noir→`G`, jaune(TX)→`R6`,
vert(RX)→`T6`, blanc(SDA)→`DA`, bleu(SCL)→`CL`. **Les couleurs du faisceau différaient du
dessin du manuel HGLRC** (paires UART et I2C miroirées) — lecture sur la sérigraphie du
module refaite deux fois puis validée par l'expérience (l'UART marche avec cette lecture).
Leçons de câblage : pads en deux rangées en quinconce (le groupe logique s'étale sur les
deux ; seule la sérigraphie fait foi), étamer tous les pads avant d'amener les fils, rangée
intérieure avant rangée du bord. Sauvetage d'un pad récalcitrant : l'étain refondu n'a plus
de flux (il se consume à la première fusion) → nettoyer le flux carbonisé (alcool), retirer
l'étain mort, repartir flux frais + étain neuf + 20-30 °C de plus sur un pad de masse.

**Le rituel multimètre a payé, deux fois.** Un vrai pont trouvé et corrigé. Puis un faux
positif instructif : bip `CAM`↔`G` avec **74,6 Ω stable** = la résistance de terminaison
vidéo 75 Ω sur la FC à l'entrée de l'AT7456E (standard analogique), pas un court. Règle
gravée : c'est **bip + ~0 Ω** qui condamne ; bip + dizaines d'Ω = électronique interne ;
chiffres qui montent = condo qui se charge. Autre subtilité apprise : une ligne UART au
repos est à 3,3 V comme l'I2C — mesurer la tension ne discrimine pas les pads.

**Test USB : GPS détecté ✅, compas muet ❌.** LEDs du M100 : bleue fixe (alim) + rouge PPS
fixe (démarré, pas de fix — normal en intérieur ; elle clignotera au fix). Après plusieurs
rebranchements du connecteur (contact de sertissage limite soupçonné — il ne parlait pas
avant manipulation) : `GPS 1: detected u-blox` + **`ROM SPG 5.10` = M10 authentique**,
auto-configuré à 230400 bauds. Le compas QMC5883, lui : `COMPASS_DEV_ID=0` persistant.
Éliminé méthodiquement : params (`COMPASS_ENABLE=1`, `COMPASS_DISBLMSK=0` — TYPEMASK
n'existe pas dans cette version), drivers (QMC5883L 0x0D **et** QMC5883P 0x2C compilés dans
le build — `.o` vérifiés), bus électrique (continuité bout en bout OK, pas de courts, repos
à 3,2 V), couleurs (validées par l'UART qui marche), étiquettes FC re-vérifiées (blanc sous
`DA`, bleu sous `CL`). Restent deux suspects : une inversion DA/CL résiduelle → **prochain
test : épissure croisée en milieu de câble** (blanc↔bleu, sans toucher pads ni connecteur —
le SH 1,0 mm sans outil d'extraction, c'est non), sinon **puce compas morte → SAV**. Pas
bloquant pour le premier vol : l'indoor n'utilise pas le compas, et l'EKF3 sait estimer le
cap sans compas dehors (GSF sur vitesse GPS).

**Divers.** MP sans fix GPS : position fantaisiste sur la carte (dérive EKF sur IMU seule)
et traits rouge/noir/orange = indicateurs cap/route pointant au nord par défaut — cosmétique.
`PreArm: RC not found` = radio éteinte pendant les tests. Implantation : TPU arrière de la
VX3.5 = passages prévus batterie/antenne VTX/brins dipôle RX (les critères d'implantation
sont une hiérarchie, pas des absolus : hélices/pincement > ciel GPS > distance fils de
puissance > antennes séparées). Les « carrés de plastique » fournis = gaine thermo à cartes
pour RX et GPS — à mouler APRÈS validation électronique complète. Le boîtier métal du TX800
= radiateur/blindage, verrouillé par les vis de montage.

**Reste à faire** : épissure croisée → verdict compas ; test LiPo caméra+VTX (image lunettes
+ OSD incrusté + test Tramp par changement de `VTX_CHANNEL` depuis MP) ; params VTX à poser
(`44`/`4`/`VTX_ENABLE=1`/`25 mW` + band/canal des lunettes) ; montage mécanique final +
gaines ; calibration compas (si vivant) APRÈS montage final ; fix GPS à la fenêtre ;
`FS_THR_ENABLE=3` + modes voie 6 (Stabilize/AltHold/Land) ; charge LiPo (source USB-C PD).
Commande en cours : gaine thermo assortie + IPA ≥90 % + kit d'extraction de contacts JST.

## 2026-07-22 — S3 : verdict compas = module défectueux (SAV), enquête close proprement

**La méthode qui a permis de conclure : le soft reboot (Ctrl-F → Reboot Pixhawk dans MP).**
Le sondage I2C du compas n'a lieu qu'au boot de la FC, et le module M100 souffre d'un
défaut de démarrage à froid (jamais détecté au power-up, il faut débrancher/rebrancher son
connecteur — comportement systématique, USB comme LiPo). Le soft reboot redémarre le
programme de la FC **sans couper les rails d'alim** → le module reste debout pendant le
nouveau sondage. Preuve dans les logs : après soft reboot, u-blox re-détecté en 9 s
directement à 230400 bauds (il avait gardé sa config → il n'a jamais perdu l'alim).

**Matrice finale, module garanti vivant au moment du sondage** : épissure croisée →
`COMPASS_DEV_ID=0` ; épissure remise droite → `COMPASS_DEV_ID=0`. Combiné aux éliminations
précédentes (params, drivers compilés, bus électrique sain, couleurs validées par l'UART),
il ne reste aucune case où un compas fonctionnel pourrait se cacher → **puce QMC morte ou
jamais reliée en interne** (ou variante M100 sans compas expédiée par erreur — à vérifier
sur la sérigraphie du blindage : « M100-5883 » vs « M100 »). Double dossier SAV : compas
muet + démarrage à froid défaillant (inutilisable en vol tel quel).

**Leçons engrangées** : soft reboot vs power-cycle (reset MCU sans couper les rails —
l'outil parfait pour re-sonder un périphérique I2C sans le redémarrer lui) ; sondage GPS
continu vs sondage compas boot-only (l'asymétrie qui masquait le défaut de démarrage du
module) ; un test dont la précondition n'est pas remplie n'est pas négatif, il est
**invalide** (la 1re épissure croisée ne testait rien : le module était couché au boot).

**Impact projet : nul.** Premier vol = indoor (Stabilize/AltHold), zéro GPS/compas requis ;
le différenciateur MTF-02P+EKF3 est par définition GPS-denied. Le module remplacé arrivera
pour les tests outdoor. La prep vol continue : test lunettes caméra+VTX + params Tramp,
montage final, FS_THR_ENABLE=3, modes voie 6.

## 2026-07-22 (suite) — Première image vidéo du drone + contrôle Tramp prouvé ✅

**Le drone voit et transmet.** Chaîne complète validée : Phoenix 2 → pad `CAM` → puce OSD
AT7456E (incrustation ArduPilot dans le signal analogique) → pad `VTX` → TX800 → 5,8 GHz →
RC832 → carte de capture MS2130 → laptop Ubuntu (`ffplay`). Image avec la neige analogique
normale du bench (antennes trop proches = saturation du récepteur ; ≥1 m et polarisations
alignées améliorent). OSD confirmé à l'écran : tension batterie + — ironie parfaite — le
« PreArm: Compass 1 not healthy » en boucle toutes les 10 s.

**Config VTX (pièges MP au passage)** : le groupe `VTX_` n'apparaît qu'après `VTX_ENABLE=1`
+ reboot ; `VTX_FREQ` est réputé *readonly* côté MP (dérivé de band/channel) ; `VTX_CHANNEL`
est **0-indexé** (0=CH1). `VTX_MAX_POWER=25` + `VTX_POWER=25` (plafond légal France).
Comportement découvert en live : à la première connexion, **le driver Tramp lit l'état réel
du VTX et adopte ses réglages** (band/channel réécrits à 0/0 = A1 5865, le défaut d'usine
du TX800) — conforme au commentaire du code (« make sure the configured values now reflect
reality »), et première preuve indirecte que le lien parlait.

**Preuve Tramp définitive** : `VTX_CHANNEL` 0→1 dans MP → l'image décroche → RC832 sur
« 12 » (A2 5845) → l'image revient. La FC pilote physiquement l'émetteur via le fil IRC
soudé sur T1. Réglage de croisière adopté : retour à A1 (0/0, RC832 « 11 ») — le défaut
d'usine comme point de ralliement prévisible. Décodage RC832 : affichage = [bande][canal],
bande 1=A … 4=F, d'où « 44 »=F4=5800 (silence, normal) et « 11 »=A1=5865 (image).

**GPS : gardé et en service.** Le démarrage à froid remarche systématiquement (la danse du
connecteur a vraisemblablement poli le contact de sertissage fautif) ; le compas reste mort
(verdict inchangé). Litige AliExpress pivoté vers **remboursement partiel sans retour** (le
retour vers la Chine tuerait l'économie du dossier). Plan B compas si besoin un jour :
breakout QMC/RM3100 à quelques euros sur les mêmes pads DA/CL. Concepts au passage : IRC
Tramp = protocole ImmersionRC de contrôle VTX (concurrent de SmartAudio/TBS), d'où le pad
« IRC » ; OSD = caractères dessinés en temps réel dans le signal vidéo par l'AT7456E.

**Position stratégique de Victor consignée (2026-07-22)** : le MTF-02P lui déplaît
(capteur cheap indoor-only — « je veux pas faire un projet jouet ») et l'idée d'un projet
sans GPS lui plaît ; accord trouvé : MTF = échafaudage sautable (décision au pied du
chapitre EKF3, chemin flow-caméra tenu prêt), le **ToF reste non-négociable** (l'échelle
d'un flow, quel qu'il soit), le GPS = instrument de mesure/vérité terrain pour les
benchmarks de dérive, pas un composant de navigation. Les deux chemins restent ouverts.

**Reste avant premier vol** : montage mécanique final + gaines thermo (RX), `COMPASS_ENABLE=0`
(tant que pas de compas vivant), `FS_THR_ENABLE=3`, modes voie 6 (Stabilize/AltHold/Land),
charge LiPo (source USB-C PD ≥65 W), checklist pré-vol.

## 2026-07-23 — Premiers sauts (!), premières analyses de logs DataFlash

**Le drone a volé** — sauts de ~10 cm au-dessus du lit (pas le protocole recommandé : 4 vis
d'hélices sur 8, en intérieur — mais il a volé, failsafe radio déclenché en vol et posé).
Montage final terminé : batterie top-mount (strap dans les fentes de la top plate, couloir
sous la plate laissé libre), TPU arrière = passages batterie/antenne VTX/brins RX comme
prévu par FlyFishRC. Leçons de montage express : hélices T-mount (l'axe central ne fait que
centrer, la fixation = 2 vis M2 par hélice — démonstration expérimentale par hélice-frisbee
au premier throttle) ; test de longueur de vis à vide (visser seule + faire tourner la
cloche = détecter le contact avant d'abîmer) ; USB seul devenu marginal depuis le montage
complet (rail 4V5 chargé RX+GPS > budget 500 mA du port → bench sur LiPo désormais).
Radio : modes confirmés voie 6 = 3 pos GAUCHE (mapping du 20/07, pas une anomalie) ; ACRO
existe sous ArduPilot (mode 1, + ACRO_TRAINER) pour plus tard. Params prep vol posés :
COMPASS_ENABLE=0, FS_THR_ENABLE=3, FLTMODE 0/2/9, failsafe batterie 14.0V→Land.

**Premier rituel data : 3 logs .bin téléchargés (MP → Download DataFlash Via Mavlink) et
analysés en pymavlink depuis WSL.** Résultats :
- **Vibrations : excellentes** — moyennes 0,1-0,9 m/s/s (seuil ~20-30), clipping ≈0 (2
  événements = réceptions sur le lit). Les plots silicone du stack fonctionnent. ✅
- **Équilibre moteurs** : 1402/1431/1421/1388 µs en poussée (~3 % d'écart) = géométrie et
  CG sains. ✅
- **Attitude** : erreur moyenne 0,7° roll / 0,3° pitch au hover — PID par défaut OK. ✅
- **Batterie** : chargée 16,5 V, sag modeste, capteur courant plausible (pics 13 A). ✅
- Dans les données : failsafe radio trigger/clear, kill switch, ELRS 250 Hz, bascule modes
  0/2/9 — tous les tests bench visibles dans les logs. Erreurs EKF/GPS-glitch en intérieur
  sans fix = normales, disparaîtront dehors.

**Feu vert vol extérieur sous DEUX conditions : 8 vis d'hélices (M2×9 validées à vide,
mais 4 manquantes — commande en cours) + coin d'herbe calme.** Protocole : Stabilize
(position basse), hover 1 m, pose, re-analyse du log (vibrations au hover soutenu = le
juge de paix). Le rituel voler→lire le log→corriger→revoler est né aujourd'hui.

## 2026-07-23 (suite) — Chapitre GPS-denied ouvert : flow réactivé dans le firmware, architecture EKF3 posée

**Firmware : l'optical flow est de retour dans le build custom.** Le `minimize_fpv_osd.inc`
des cartes 1 Mo force `AP_OPTICALFLOW_ENABLED 0` → réactivé dans le hwdef
`SpeedyBeeF405Mini` de la branche `argos-custom`, en ne gardant que le **backend MAVLink**
(`AP_OPTICALFLOW_MAV_ENABLED 1`, les drivers de capteurs SPI/série qu'on ne possède pas
restent dehors). Vérifié dans les sources : `EK3_FEATURE_OPTFLOW_FUSION` suit ce flag
automatiquement → la fusion EKF3 + l'estimateur de hauteur-sol reviennent avec. Réactivé
aussi **FlowHold** (mode 22 : tenue de position au flow SANS télémètre — le premier barreau
de l'échelle de vol, testable avant même l'arrivée du ToF). Build vert :
**874 696 B utilisés / 124 728 B libres** — tout tient large dans le 1 Mo. Deux pièges
hwdef appris : (1) pour écraser un `define` posé par un include, il faut `undef` d'abord
(le premier `define` gagne, silencieusement) ; (2) `hwdef.h` n'est régénéré qu'au
`waf configure`, pas au build incrémental. Modif stagée — commit à faire, puis rebuild
(pour le hash de traçabilité) et flash via `ARGOS_firmware\`.

**La contrainte qui a structuré toutes les décisions capteurs : il ne reste QU'UN seul
UART full-duplex libre (UART3).** UART4 = Bluetooth interne sans pads, UART5 = pad RX
seul (télém ESC). Conséquences en cascade, tranchées aujourd'hui :

- **ToF = Benewake TFmini-S en mode I2C** (~40 €) sur les pads DA/CL (libres, le compas
  mort n'écoute que 0x0D, le TFmini-S parle en 0x10). `RNGFND1_TYPE=25` (driver
  TFminiPlus-I2C **déjà compilé** dans le build minimisé), 12 m, tient le plein soleil.
  Coût UART : zéro. Écarte TF-Luna (UART-only sous ArduPilot) et VL53L1X (aveugle au
  soleil = indoor-only). Subtilité : livré en mode UART → une commande de bascule I2C à
  envoyer une fois via adaptateur USB-TTL.
- **MTF-02P : ÉCARTÉ** — décision actée (position de Victor + argument structurel : il
  prendrait UART3, la place du companion qu'il était censé dé-risquer). Sa valeur de
  dé-risquage est remplacée gratuitement par l'échelle SITL. Tripwires consignés dans le
  doc pour rouvrir le dossier (qualité flow inutilisable, latence > 250 ms, ou envie du
  head-to-head) — et s'il revient un jour, il se branchera sur un UART du Pi, pas de la FC.
- **Companion = Raspberry Pi Zero 2 W sur UART3 + caméra Arducam OV9281 global shutter
  pointée au nadir** (~55 € l'ensemble + BEC 5 V dédié). Triple rôle : calcul du flow
  (LK sparse 30-50 Hz), injection `OPTICAL_FLOW` en MAVLink2 local (~30-45 ms de latence,
  loin du plafond `EK3_FLOW_DELAY=250 ms`), et pont télémétrie WiFi (mavlink-router) —
  **il absorbe le rôle prévu de l'ESP32 DroneBridge**. Point clé compris en route : la
  Phoenix2 FPV ne peut PAS être la caméra de flow (elle regarde devant ; le modèle EKF3
  suppose un capteur nadir) — elle reste 100 % pilotage/OSD/Mode A.

**Architecture EKF3 gelée dans [`docs/ekf_flow_fusion.md`]** (le doc interview-gold est
né) : table `EK3_SRC1_*` flow-only (VELXY=5, POSXY=0, POSZ=1 baro + terrain estimator),
`EK3_SRC2` = GPS en filet de sécurité commutable (`RCx_OPTION=90`) jamais utilisé pendant
les runs de benchmark, GPS = vérité terrain loggée. Pépite vérifiée dans
`AP_NavEKF3_Control.cpp` : le flow est AID_RELATIVE et le code dit explicitement que les
capteurs body-frame **n'exigent pas d'alignement de yaw** → voler flow-only SANS compas
(`EK3_SRC1_YAW=0`, cap intégré gyro) est une config supportée — le compas mort ne bloque
rien. Côté injection, lu dans `AP_OpticalFlow_MAV.cpp` : envoyer les champs
`flow_rate_x/y` (rad/s, float) — le backend les préfère aux champs legacy — plus
`quality` 0-255 qui gate la fusion ; l'horodatage est à la réception (jitter non corrigé
→ cadence d'envoi stable). Méthode de mesure de la latence : corrélation croisée
`OF.flowX` vs gyro dans les logs (le pic donne `EK3_FLOW_DELAY`, son signe valide les
conventions d'axes — l'échec classique du flow étant l'axe inversé).

**Échelle de validation SITL-first** : (1) flow simulé SITL → valider tout le set
`EK3_SRC*` et FlowHold/Loiter sans matériel ; (2) script injecteur `OPTICAL_FLOW` → SITL
= répétition du chemin d'injection ; (3) caméra Gazebo → le VRAI algo de flow → EKF3 SITL
= répétition générale logicielle du différenciateur sur le rig existant ; (4) wiggle test
au bench (signes + délai) ; (5) vol : FlowHold → Loiter flow-only → benchmark de dérive
p50/p95 vs GPS sur 60 s × ≥10 runs. Liste de courses complète en fin de doc.

## 2026-07-23 (soir) — Premier vol EXTÉRIEUR + panne baro diagnostiquée (court I2C)

**Premier vol en extérieur — il a volé**, malgré une série de soucis (et un pitch inversé
piloté sans le savoir, posé quand même). Observations de vol, à confirmer au prochain log :
- **Wobble circulaire lent** persistant même en hauteur avec 8 vis (donc ni effet de sol ni
  vis) → hypothèse CG : batterie trop en avant et mal calée. À vérifier via RCOU.
- **Manche droit avant = recule** → pitch inversé. Fix : `RC2_REVERSED=1` + vérif sens dans
  MP avant de revoler (vrai point sécurité).
- **Descente bizarre** : « ne descend pas, tient l'altitude, puis chute d'un coup moteurs
  réduits » = comportement typique **AltHold** (manche centré = tient, faut descendre sous
  le centre) → il était probablement sur la position AltHold du sélecteur, pas Stabilize.
  OU le baro déconnait déjà en vol et nourrissait AltHold en altitude foireuse. Log à lire.
- **Drift gauche constant** → CG/trim, à confirmer.
- **GPS** : pour la 1re fois LED fixe + LED clignotante = **vrai fix 3D** (log au sol de
  17h49 : 14 sats, status 4). Puis le connecteur s'est débranché en vol (crimp limite connu)
  → retiré. Le module GPS marche ; c'est le connecteur le point faible.

**LA panne : `Config Error: Baro: unable to initialise driver` + `motors not allocated`**
(refus d'arm, OSD disparu de la vidéo en vol, logs non téléchargeables — TOUS des symptômes
AVAL de la même erreur : sur config error ArduPilot stoppe son init). **Cause trouvée et
confirmée** : le baromètre DPS310 est sur **I2C1**, et cette carte n'a **qu'un seul bus I2C**
(`I2C_ORDER I2C1`, `BARO ... I2C:0:0x76`, `HAL_I2C_INTERNAL_MASK 0`) — le **même** que les
pads DA/CL du GPS. Les **épissures croisées blanc/bleu (= SDA/SCL) faites pour le test compas
étaient mal isolées** → en court sur le bus (entre elles ou contre le carbone) → baro mort.
Victor a écarté les épissures → baro revenu (`Barometer 1 calibration complete`, `ArduPilot
Ready`). Diagnostic bouclé par lui-même. **Action : isoler proprement ou dessouder les 6 fils
GPS** (module sorti + compas mort = ils ne servent plus). Leçon gravée : sur cette FC, tout
défaut sur DA/CL tue le baromètre — l'I2C est un bus partagé, pas une ligne dédiée au GPS.

**Logs de vol probablement perdus** : rien de la séance de vol (~18h) sur disque ; la puce
DataFlash de 8 Mo était vraisemblablement pleine (matin + aprem) → vol non enregistré. À
vérifier via la liste des logs sur la puce ; sinon Erase pour repartir propre. Les questions
wobble/drift/altitude tomberont au prochain vol (avec pitch corrigé).

**Bilan** : le drone vole en extérieur, la propulsion et la radio tiennent, mais premier vol
= premier crash-course de debug terrain. Rien de cassé : une panne d'isolation, résolue.

## 2026-07-23 (nuit) — Analyse du log de vol extérieur : diagnostic complet

Le vol EST récupéré : `2026-07-23 17-51-32.bin` (5,6 Mo, 790 s, 15 sats, décollages
multiples, altitude −3,5→5,7 m). Les fichiers « 1970 » sur la puce n'étaient pas vieux —
Victor a failli les jeter sur la foi du nom ; c'est le CONTENU qui tranche (rappel : nom de
log ≠ contenu). Les 2 tlogs (18h56/19h01) sont inutiles = MP branché après le crash baro.

**Lecture en contexte (la leçon du jour) :** le log affichait VibeMax 52 + clipping 35 →
l'air alarmant. Corrélation pic-de-vibe ↔ état : **10 pics >25, TOUS au sol/atterrissage,
0 en vol** = rebonds de posé, pas une vibration de vol. En vol : moyennes 0,7-1,1 (saines),
moteurs équilibrés ~2 %, batterie OK (I max 29,6 A). **Mécaniquement le drone va bien.**

**Vraies causes des symptômes :**
1. **Wobble circulaire → pitch inversé** (n°1). Manche tangage à l'envers → chaque correction
   part à l'envers → oscillation entretenue (+ CG décalé + vent). Fix : `RC2_REVERSED=1` +
   vérif sens dans MP.
2. **Glitches EKF EN vol → capteurs I2C/GPS branlants.** Timeline : 74s EKF cale sur GPS →
   79s `EKF variance: position lost` (GPS qui lâche, connecteur) → 80s `DCM Roll/Pitch
   inconsistent 47°` → 202s+ `EKF attitude is bad`/`core unhealthy` (refus d'arm). Le GPS au
   connecteur branlant + le baro aux épissures I2C nues nourrissaient l'EKF en données
   pourries → attitude fausse (aggrave le wobble) → arm refusé. Le baro mort au sol à 19h04 =
   stade terminal de ce qui clignotait déjà en vol.
3. **Descente « tient puis chute » → AltHold.** Modes enregistrés : passages en AltHold
   (mode 2) confirmés. Manche centré = tient l'altitude ; + baro instable = altitude tenue
   fausse → chute. Pas une erreur de pilotage.

**Plan avant de revoler (rien de cassé) :** (1) `RC2_REVERSED=1` + vérif ; (2) nettoyer le
câblage GPS I2C — isoler OU dessouder les 6 fils (GPS sorti + compas mort + connecteur
branlant → retrait = le plus propre, supprime mort-baro ET glitches EKF) ; (3) effacer la
puce log (8 Mo pleine) ; (4) prochain vol Stabilize seul, sauts courts, relire vibe-en-vol +
santé EKF ; AltHold seulement quand baro prouvé stable. Outil `tools/log_quicklook.py` étendu
mentalement (corrélation vibe/état de vol = le bon réflexe d'analyse).

## 2026-07-23 (nuit, correction) — Wobble : mes 2 théories réfutées, retour à l'honnêteté

Victor a challengé (à raison) : le wobble était là AUSSI le matin en intérieur, GPS sans
fix → le GPS ne peut pas être la cause commune. Vérification poussée du log du matin
(11-35) : les gros roulis (jusqu'à +160°) surviennent à ~2 m d'altitude, montée régulière
+47→+160° en 0,5 s avec perte d'altitude = **de vrais RETOURNEMENTS**, pas des erreurs
d'estimation ni du bruit capteur. → Mes deux hypothèses tombent : (1) GPS réfuté par le
matin ; (2) « estimation corrompue par capteurs I2C » réfuté = les gros chiffres sont des
flips réels, pas des glitches. J'ai fait du pattern-matching sur des logs de sauts bordéliques
(flips + rattrapages + rebonds + manipulation) — indiagnostiquable pour un « petit wobble ».

**Position honnête retenue** : le wobble léger d'une montée est le plus probablement le
**tune par défaut non adapté** à cette frame 3,5"/3800KV/hélices (cas archi-courant d'un
premier vol non tuné), possiblement + CG décalé. Le baro/GPS branlants restent un problème
RÉEL mais SÉPARÉ (mort baro + EKF-position dégradé l'aprem, pas le wobble). Pour trancher :
il faut UN log propre = hover stable 20-30 s en Stabilize (pas des sauts).

**Plan** : (1) nettoyer/retirer fils GPS I2C ; (2) recalibration accéléro à froid/à plat
(masse changée depuis le 21/07) ; (3) `RC2_REVERSED=1` (pitch, certain indépendamment du
wobble) ; (4) EKF vert au sol avant arm ; (5) hover soutenu → lecture log ENSEMBLE ; (6) si
wobble persiste sur hover propre → Autotune. Leçon perso (Claude) : ne pas surinterpréter un
log sale ; un chiffre d'attitude énorme peut être un vrai flip, pas un bug capteur — vérifier
l'altitude/contexte AVANT de conclure. (Et créditer Victor, pas moi, pour le doute qui a
cassé la fausse piste.)

## 2026-07-23 (nuit, résolution) — Wobble PARTI : les épissures I2C nues étaient (probablement) la cause

Test indoor après 2 changements : (1) isolation des épissures SDA/SCL qui étaient scotchées
ensemble SANS isolation entre elles, (2) batterie repositionnée. **Le wobble a disparu.**

**Victor avait raison, et ma conclusion « tune par défaut » était à côté.** Le vrai facteur
COMMUN au wobble matin+aprem = les épissures I2C nues (présentes les deux fois) — que j'avais
éliminées à tort. Mécanisme que j'avais loupé : un bus I2C en court/qui accroche ne perturbe
pas que le baro, il fait **hoqueter la boucle de contrôle** (timeouts I2C → jitter du loop
d'attitude → oscillation). Deux fils SDA/SCL qui se touchent par intermittence = wobble
plausible. Réserve : 2 variables changées (fils + batterie) → pas 100 % attribuable ; le log
propre de demain (hover soutenu 20-30 s dehors) confirmera.

**Nouveau symptôme, cause identifiée par Victor** : drift franc vers l'ARRIÈRE = CG trop
reculé (batterie déplacée en arrière → poussée devant le poids → nez cabré → recule). Fix :
avancer la batterie, test d'équilibre sur un tube au centre (doit rester à plat), marquer la
position au scotch. Rappel : petit drift résiduel = normal en Stabilize (pas de tenue de
position) ; c'est le drift franc et constant qu'on corrige.

**État** : pitch corrigé (RC2_REVERSED), fils I2C isolés, wobble parti en test indoor, EKF
Velocity/PosHoriz rouges = NORMAL sans GPS (n'affecte pas Stabilize/AltHold — mon « EKF tout
vert » était faux pour un setup sans GPS ; le vrai check attitude = HUD qui suit les
basculements + arme en Stabilize). Reste : recentrer CG, puis vol extérieur = hover soutenu
→ log à lire ensemble. Autotune seulement après ce hover propre si un résidu de wobble reste.

## 2026-07-23 (nuit, point d'étape) — Wobble intermittent, moteurs OK au banc, verdict reporté au vol dehors

Suite : wobble INTERMITTENT en intérieur (« des fois oui des fois non »), test moteurs
individuels dans MP = les 4 sonnent/tournent pareil → piste moteur écartée (log confirmait
déjà des commandes équilibrées ; rappel : pas de télémétrie RPM par moteur sur BLHeli_S
stock → un fil moteur mal soudé serait quasi INVISIBLE au log, d'où le test au banc). Le
log ne peut pas diagnostiquer le wobble (pas de RPM, RCOUT = commandes pas poussée réelle,
écarts-types des 4 moteurs proches 77-98µs = aucun coupable).

**Point clé : l'intérieur est un mauvais juge.** Un petit quad en chambre = son souffle
recircule (sol/murs/plafond) et le buffete → il danse indépendamment du tune. « Parfois ça
wobble parfois non » en déplaçant le drone = signature de l'aérologie de la pièce, pas une
conclusion sur la stabilité. → verdict reporté au **vol extérieur** (air calme, CG recentré,
hover soutenu 20-30s à 1,5-2m, log lu ensemble). Hypothèses restantes : tune par défaut trop
chaud pour un 3,5" (principale, vu le « wobble circulaire ») OU juste l'intérieur. Si
oscillation régulière sur hover propre dehors → Autotune. État matériel : pitch corrigé
(RC2_REVERSED), fils I2C isolés, moteurs OK, CG à recentrer. Prochaine session = ce vol test.

## 2026-07-24 — Wobble mesuré (0,7 Hz) = tune par défaut → cap sur l'Autotune

Vol dehors ce matin : wobble PERSISTE en air libre (→ pas la chambre) ET identique avec un
câble moteur détaché en vol (→ pas les moteurs ; Victor a ressoudé). Diagnostic mécanique
définitivement écarté. Params lus dans le log : **100 % défauts d'usine ArduCopter**
(ATC_RAT_RLL_P=0.135, D=0.0036, ANG_P=4.5, INS_GYRO_FILTER=20) — jamais tuné.

**Wobble enfin mesuré** : dans le log 10-17, fenêtre de 21 s tenue sous ±25° → oscillation
**~0,7 Hz, Roll ±13°, DesRoll≈0°** (le drone oscille seul, manches centrés). Oscillation
LENTE = domaine du réglage de contrôle (pas bruit HF/filtre). Signature classique « vole mais
wobble sur tune défaut d'un 3,5" léger » (les défauts sont calibrés pour du 5"+). Note :
extraire un wobble propre a demandé de chercher la fenêtre calme — les logs sont pollués de
tumbles (±358° roll) dus au câble détaché + arrêts brusques.

**Décision : Autotune** (le drone est assez pilotable, 21 s à ±25°). Deux préparatifs
d'abord : (1) **recentrer le CG** (batterie avancée + test d'équilibre — un CG décalé biaise
le tune, et pourrait déjà réduire le wobble) ; (2) vérifier qu'**AltHold tient l'altitude**
(baro réparé). Procédure : `RC7_OPTION=17` (Autotune sur la voie 7 libre), `AUTOTUNE_AXES=7`,
décollage AltHold ~3-4 m en zone dégagée air calme, bascule voie 7, saccades auto 5-10 min,
atterrir en gardant la voie active pour sauver. Sécurité : pouce prêt à repasser Stabilize +
kill. Plan B si Autotune galère : baisser le rate P à la main d'abord. Log post-Autotune à
lire ensemble. Outil : `tools/log_quicklook.py` (+ analyses ad hoc fréquence d'oscillation).

## 2026-07-24 (après-midi) — PERCÉE : le vrai coupable = VIBRATIONS mécaniques (balourd), pas le tune

Session Autotune ratée mais ULTRA diagnostique (log 14-07-40, 8 Mo). Autotune n'a jamais
démarré (« Mode change to Autotune failed: init failed » ×2). Mais 2 symptômes-clés
rapportés par Victor + confirmés au log ont tout recadré :

**1. Envolée AltHold incontrôlable.** CTUN à l'appui : de t=87 à t=90s, BAlt passe de -1m à
14,3m en 3s, ALORS QUE le manche gaz est à fond en bas (ThIn=-1.6). Le contrôleur d'altitude
reçoit des **données verticales corrompues** → croit que le drone tombe → plein gaz → monte →
emballement. Dangereux (a causé une chute + hélice tordue).

**2. Vibrations qui montent avec le régime.** VibeZ moy 1,2 à bas régime → **5,8 à haut
régime** (pics 49, means X/Y/Z 3,4/4,9/4,1, clipping 0). Vibration proportionnelle au régime
= **balourd mécanique** (hélice ou moteur déséquilibré). = le « shake » ressenti.

**Recadrage majeur : ce n'était JAMAIS le tune.** C'est mécanique depuis le début, ce qui
explique pourquoi baisser rate P, angle P, etc. n'a jamais rien changé. Les vibrations
corrompent (a) l'estimation verticale → envolée AltHold, (b) l'estimation d'attitude →
wobble/shake. Le « changer les hélices ne change rien » colle SI le balourd est dans un
**MOTEUR** (arbre tordu par une chute) et pas l'hélice. Les chutes répétées ont pu
l'aggraver. Note : EKF GPS-aiding instable aussi (yaw GPS sans compas + connecteur branlant)
→ contribue au drift incohérent + yaw incohérent, mais problème séparé.

**PLAN (sécurité d'abord : STABILIZE UNIQUEMENT, plus d'AltHold/Autotune tant que vibrations
pas réglées — l'envolée est dangereuse) :** (1) remplacer les 4 hélices (celle retordue à la
main = déséquilibrée) ; (2) inspecter chaque MOTEUR à la main — arbre droit ? roulement qui
gratte/du jeu ? un arbre tordu survit aux changements d'hélice = suspect n°1 ; (3) re-hover
Stabilize → relire VibeZ au log ; (4) vibrations basses → réévaluer wobble → alors seulement
AltHold + Autotune. Hypothèse : cause UNIQUE (balourd) derrière shake + envolée AltHold +
wobble. Aussi : puce log pleine (« logging full ») → à effacer.

## 2026-07-25 — CAUSE RACINE TROUVÉE : le mélangeur sature en permanence (drone trop surmotorisé pour l'échelle de poussée ArduPilot)

Relecture complète des 13 logs DataFlash (`/mnt/c/.../Mission Planner/logs/QUADROTOR/1/`)
+ lecture du code d'`AP_MotorsMatrix` / `AC_AttitudeControl_Multi` dans MON fork. Verdict :
**ce n'était ni le tune, ni un balourd, ni le baro.** Une seule cause explique tout.

### La mesure

Sur les 3 vols exploitables (07-23 17-51, 07-24 10-17, 07-24 14-07), en hover stabilisé :

| grandeur | mesure |
|---|---|
| sortie moteur au hover | **1246-1264 µs** |
| `MOT_SPIN_MIN` = 0.15 | = 1150 µs → **96 µs de marge sous le hover** |
| poussée de hover réelle (échelle 0→1 d'ArduPilot) | **0.052** |
| `MOT_THST_HOVER` | **0.35** → faux d'un facteur **6,8** |
| drapeau `LIMIT` de `PIDR/PIDP/PIDY` (bit 0 = sortie saturée, anti-windup actif) | **98-99,4 % du temps, sur TOUS les vols depuis le premier** |
| ≥1 moteur collé sur `MOT_SPIN_MIN` | **100 % du temps** |
| terme I des boucles de rate | **≈ 0** (std 0.0002) alors que l'erreur d'assiette moyenne est de **+6° roulis / +8° tangage** |
| gaz demandés vs appliqués (`CTUN.ThO` vs `MOTB.ThrOut`) | 0.024 → 0.056 = **x2,4** |

### Le mécanisme (vérifié dans le source, pas déduit)

`AP_MotorsMatrix::output_armed_stabilizing()` : quand la commande roulis+tangage+lacet ne
tient pas dans la plage de poussée disponible, il calcule
`rpy_scale = -throttle_avg_max / rpy_low`, applique `limit.set_rpy(true)`, et sort
`_thrust_rpyt_out[i] = throttle_best + rpy_scale * _thrust_rpyt_out[i]`.

Trois conséquences, toutes observées :

1. **Les gains PID deviennent mathématiquement sans effet.** Si on double la sortie des PID,
   `rpy_low` double, donc `rpy_scale` est divisé par deux : le produit `rpy_scale * rpy_out`
   est **inchangé**. → C'est LA raison pour laquelle baisser rate P (0.135→0.08→0.06) ou
   angle P (4.5→3.0) n'a jamais rien changé. Ce n'était pas « le tune est déjà bon », c'était
   « le tune est hors circuit ». (Note : aucun des 13 logs ne contient de gains modifiés —
   tous à 0.135/4.5 — donc ces essais n'ont de toute façon jamais été enregistrés.)
2. **Les intégrateurs sont gelés** (`_motors.limit.roll` est passé en argument `limit` à
   `AC_PID::update_all`). Plus de trim → erreur d'assiette permanente 6-8° → **dérive
   constante, de direction variable**. Rien à voir avec le CG.
3. **La poussée moyenne n'est plus pilotée par le manche** mais par
   `get_throttle_avg_max() = throttle_in*(1-mix) + MOT_THST_HOVER*mix`.
   - Stabilize (`ATC_THR_MIX_MAN`=0.1) : plancher = 0.35×0.1 = **0.035 > hover réel 0.026**
     → le drone monte même gaz fermés, d'où le hover à **6,5 % de course de manche**.
   - AltHold (`ATC_THR_MIX_MAX`=0.5) : plancher = 0.35×0.5 = **0.175 = 7x le hover réel**.

### L'envolée AltHold, expliquée à la seconde près (log 14-07, t=188.57)

Bascule en AltHold → `MOTB.ThrOut` saute à **0.1750** (= 0.35 × 0.5, exactement) et **y reste
figé 1,3 s** pendant que `CTUN.ThO` (gaz commandés) est à **0.0000** et que le manche est en
bas. BAlt : 3,97 → 13,68 m, soit **+7,5 m/s**. Ce n'était PAS une estimation verticale
corrompue par les vibrations : c'est un feed-forward de gaz faux d'un facteur 7.

Pire : `AP_MOTORS_THST_HOVER_MIN = 0.125f` est un **clamp dur** dans
`get_throttle_hover()`. Avec un hover réel à 0.052, `MOT_THST_HOVER` ne peut PAS descendre
assez bas → **AltHold est structurellement impossible dans cette config**, même avec
l'apprentissage. Et `Copter::update_throttle_hover()` sort tôt si
`flightmode->has_manual_throttle()` → **il n'apprend jamais en Stabilize**. Cercle vicieux :
AltHold est cassé parce que THST_HOVER est faux, et THST_HOVER ne peut s'apprendre qu'en
AltHold.

### Ce que ça invalide

- **« Balourd mécanique » (07-24 après-midi) : FAUX.** Un balourd d'hélice/moteur apparaît à
  1x le régime = ~245 Hz au hover ici (24,6 % de gaz × 15,7 V × 3800 KV). Les VIBE qui
  montent avec le régime (1,2 → 5,8) sont le comportement NORMAL de n'importe quel quad, et
  5,8 est très bon (seuil d'alerte ArduPilot : 30). Clipping = 0 en vol. Rien à changer.
- **« Tune par défaut » (07-24 matin) : FAUX** (les gains sont hors circuit).
- **« Le baro fait s'envoler AltHold » : FAUX** pour l'envolée. Mais voir ci-dessous.
- **Le « 0,7 Hz » mesuré : ARTEFACT.** ATT/RATE sont loggés à 10 Hz (Nyquist 5 Hz), IMU à
  25 Hz — et il y a ~23 % de messages perdus. Le spectre du gyro roulis a **97 % de son
  énergie dans la bande 8-12,5 Hz, empilée sur Nyquist** = signature d'aliasing. La vraie
  fréquence est probablement ~15 Hz (repliement 25-9,6), mais **elle n'est pas mesurable
  avec ce réglage de log**. `GyrX` rms = 2,06 rad/s (118 °/s) contre 0,65 pour `GyrY` en
  fenêtre calme : il reste une oscillation roulis HF réelle à requalifier une fois la
  plage de poussée corrigée et le logging réparé.

### Problème secondaire RÉEL : le baro voit le souffle des hélices

Test sur 5 logs, drone au sol, altitude vraie constante, moteurs OFF vs moteurs au ralenti :
**BAlt chute de 0,86 à 1,68 m** rien qu'au ralenti. Le baro (DPS310) n'est pas bruité
(0,12 m statique) mais **biaisé par le débit d'air**. À traiter (mousse à cellules ouvertes
sur le capteur) avant de compter sur AltHold — mais ce n'est PAS la cause de l'envolée.

### Plan de correction (ordre impératif)

1. **Calibration `MOT_SPIN_ARM` / `MOT_SPIN_MIN`** (procédure ArduPilot, MP → Motor Test).
   Attendu ~0.02-0.04 et ~0.04-0.06, pas 0.10/0.15 (valeurs par défaut dimensionnées pour du
   5"+ lent). Vérifier que les 4 moteurs démarrent et tournent **sous charge** sans décrocher.
2. **`MOT_THST_EXPO` ≈ 0.55** (0.65 = valeur pour grosses hélices).
3. **Vol Stabilize court → relire la sortie moteur au hover → recalculer** avec
   `tools/thrust_range.py`. Si la poussée de hover est encore < 0.20, **plafonner
   `MOT_SPIN_MAX`** (~0.57-0.60 attendu, soit 1570-1600 µs) : on sacrifie une poussée max
   dont il y a un excès absurde (T/W restant ≈ 4).
4. **`MOT_THST_HOVER` = valeur mesurée** (cible 0.25). Ensuite seulement AltHold devient sûr,
   et `MOT_HOVER_LEARN=2` pourra l'affiner tout seul.
5. **⚠ SÉCURITÉ — baisser les gains AVANT de revoler.** Aujourd'hui le gain effectif est
   `P × rpy_scale ≈ 0.135 × 0.1`. Une fois la saturation levée, `rpy_scale → 1` : le gain
   d'assiette réel est multiplié par ~8-10 d'un coup. Utiliser **MP → SETUP → Mandatory
   Hardware → Initial Tune Parameters** (hélice 3,5", 4S) qui pose d'un bloc `ATC_RAT_*`,
   `INS_GYRO_FILTER` (20 Hz est trop bas pour du 3,5") et `MOT_THST_EXPO`. Premier vol : bas,
   court, sur herbe, pouce sur le kill.
6. **Réparer le logging avant de rediagnostiquer** : `LOG_BITMASK` + bit 0 (`ATTITUDE_FAST`,
   ATT/RATE/PID à la cadence de boucle) et décocher GPS/COMPASS/NTUN/CAMERA pour tenir dans
   les 8 Mo ; pour une vraie FFT de vibration, `INS_LOG_BAT_MASK=1` (échantillonneur par
   lots, ~1 kHz brut) sur un vol de 60-90 s, puce effacée avant.
7. Puis, dans l'ordre : hover propre → relire le wobble résiduel → notch harmonique
   (`INS_HNTCH`) si besoin → **Autotune**.

Outil ajouté : **`tools/thrust_range.py`** — inverse la courbe de poussée d'ArduPilot pour
sortir la poussée de hover réelle, le % de saturation du mélangeur et la valeur de
`MOT_SPIN_MAX` à viser. À relancer après chaque changement.

**Leçon de méthode.** J'ai enchaîné 4 diagnostics faux (pitch inversé → I2C → tune → balourd)
en raisonnant sur des symptômes et des moyennes. Ce qui a tranché, c'est (a) d'aller lire les
drapeaux d'état du contrôleur dans le log (`PIDR.Flags`) plutôt que ses sorties, (b) de lire
le code du mélangeur au lieu de supposer ce qu'il fait, et (c) de vérifier la cadence
d'échantillonnage avant de croire une fréquence. Un log qui « montre » 0,7 Hz à 10 Hz
d'échantillonnage ne montre rien.

---

## 2026-07-25 (suite) — Réorganisation du portfolio en 3 groupes + `GUIDED_NOGPS` identifié

Session sans code : remise à plat de la stratégie, déclenchée par la fragilité du 3,5" (pas de
budget si quelque chose casse → tout faire dépendre de lui met le portfolio sur un point de
défaillance unique). Cadre acté dans **`PORTFOLIO.md`** (privé, gitignoré comme
`argos-plan-sprint.md`).

**Structure retenue : 3 groupes indépendants**, par régime de risque — (1) ARGOS sur le 3,5" +
SITL/Gazebo, (2) le whoop Air65 II sous Betaflight comme plateforme d'itération IA sans peur de
la casse, (3) recherche en simu pure. Règle qui en découle : **le 3,5" n'est pas une plateforme
d'itération, c'est une plateforme de tournage.**

### La trouvaille technique : `GUIDED_NOGPS`

Cherchant comment faire l'engagement sur cible **sans GPS et sans MTF-02P**, j'ai identifié le
mode **20** (`ArduCopter/mode.h:95`) : un Guided réduit au contrôle d'attitude pur — `init()`
appelle `angle_control_start()`, `run()` appelle `angle_control_run()`, rien d'autre
(`mode_guided_nogps.cpp:10-23`). La boucle devient `erreur pixel → angles désirés + Δcap →
quaternion → SET_ATTITUDE_TARGET`, **sans aucune estimation de position**.

Spécification lue dans le source (`ArduCopter/GCS_MAVLink_Copter.cpp:890-964`) — **deux points
où la doc communautaire est fausse** :

- **les rates de corps SONT supportés**, mais en tout-ou-rien : les trois bits `*_RATE_IGNORE`
  clairs, ou les trois posés. Un mélange → `hold_position()`. Donc `type_mask = 0b00000111`
  (angles seuls) est un choix, pas une limite du firmware ;
- **la sémantique de `thrust` dépend de `GUID_OPTIONS` bit 3**
  (`SetAttitudeTarget_ThrustAsThrust = 1U << 3`, valeur 8, `mode.h:1207`). Bit à **0 (défaut) =
  taux de montée** : `0.5` tient l'altitude, `>0.5` monte jusqu'à `WP_SPD_UP`, `<0.5` descend
  jusqu'à `WP_SPD_DN` → **ArduPilot ferme la boucle d'altitude au baro, sans GPS**. Bit à 1 =
  poussée brute contrainte à −1..1.

Le champ `thrust` est **obligatoire** (`THROTTLE_IGNORE` posé → `hold_position()`), et le
quaternion doit être unitaire à ±1e-3 sous peine du même sort.

**Conséquence qui unifie les 3 groupes :** `GUID_OPTIONS` bit 3 donne exactement l'espace
d'action *attitude + poussée brute* des papiers de RL drone — et c'est le même que Betaflight en
ACRO. Une politique apprise a donc la même interface sur les trois plateformes.

**Deux difficultés de conception identifiées** (c'est là qu'est le contenu) : (1) sans retour de
vitesse il n'y a pas d'amortissement → le drone dépasse ; la **taille de la bounding box sert de
capteur de distance** pour la loi de garde ; (2) compas mort + pas de GPS → le cap dérive, donc
**envoyer `cap_courant + Δ`** et jamais un cap absolu.

**Prérequis matériel :** l'altitude tenue à `thrust = 0.5` s'appuie sur le baro, qui est biaisé
par le souffle des hélices (0,86 → 1,68 m au ralenti, cf. `plan_correction_poussee.md`). La
mousse sur le DPS310 devient un **prérequis** de ce bloc, pas une finition.

### Autres points tranchés

- **HITL, échelle honnête.** HITL-1 companion-in-the-loop (vrai Pi Zero 2W, vrai UART, vrai
  MAVLink contre Gazebo/SITL) = certain et le plus utile ; HITL-2 radio en joystick USB = facile ;
  HITL-3 Simulation on Hardware (`Tools/scripts/sitl-on-hardware/`) = **pronostic négatif** :
  cibles de référence MatekH743/CubeOrange (H7, 2 MB) qui désactivent déjà NavEKF2/ADSB/proximity
  /visualodom pour tenir, alors que le build ARGOS est à 874 696 B utilisés / 124 728 B libres sur
  1 MB. Dépassement attendu ~200-300 KB. Le test coûte une commande — on lira le chiffre.
- **ArduPilot sur l'Air65 II : impossible.** Matrix 1S 5IN1 II = **STM32G473CEU6**, 512 KB de
  flash, **pas de baro**. ArduPilot n'a aucune cible ArduCopter sur G4 (le G4 n'existe chez eux
  que pour AP_Periph). Et c'est tant mieux : Betaflight en ACRO donne l'interface CTBR brute,
  meilleure pour une politique apprise.
- **MAVLink en profondeur** : dialecte `argos.xml` + `ARGOS_TARGET`, généré en Python **et** en C
  depuis la même source (`mavgen --lang=Python` / `--lang=C`), plus un panneau console qui fait
  **inspecteur ET composeur** (montant : ID/nom/Hz/champs/hexa ; descendant : formulaire →
  message → `COMMAND_ACK`) — un outil qui couvre les deux sens plutôt que deux demi-outils.
- **Écarté : le debugging comme artefact de portfolio.** Position de Victor, et elle est juste :
  c'est une ligne de CV et de la matière d'entretien. Un projet qui montre une capacité réelle
  implique ces phases par construction ; en faire un livrable séparé signale que c'est le point
  haut.

**Prochain pas :** sortir le drone de la saturation de poussée, et en parallèle — sans aucune
dépendance matérielle — `GUIDED_NOGPS` en SITL.

### Contre-vérification (même jour) — trois corrections

**1. `GUIDED_NOGPS` n'était pas dans le firmware.** `minimize_common.inc:115` :
`define MODE_GUIDED_NOGPS_ENABLED 0`. Exactement le piège FlowHold, sur la brique centrale du
Groupe 1 : le drone aurait été flashé, emmené sur un terrain, et le mode 20 n'aurait pas existé
dans la liste. Corrigé dans le hwdef `SpeedyBeeF405Mini` (recette `undef` puis `define`, cf. le
piège « le premier define gagne silencieusement »), rebuild vérifié : **9 symboles
`ModeGuidedNoGPS`, coût 288 B, 124 440 B libres** — le mode réutilise le contrôleur d'angle de
Guided qui était déjà là.

Nuance qui aggrave le piège : le handler MAVLink n'est **pas** gardé par
`MODE_GUIDED_NOGPS_ENABLED` mais par `#if MODE_GUIDED_ENABLED`
(`GCS_MAVLink_Copter.cpp:889` et `:1181`). Le point d'entrée `SET_ATTITUDE_TARGET` aurait donc
répondu normalement pendant que le mode manquait → paquets acceptés et silencieusement ignorés.
Vérifié présent dans le binaire : `handle_message_set_attitude_target` et
`set_attitude_target_provides_thrust`. **Le SITL construit tout, donc développer la loi en SITL
n'aurait rien révélé — la divergence n'apparaît qu'au flash.** Argument de plus pour le HITL.

**2. Erreur corrigée dans `PORTFOLIO.md` : CTBR ≠ attitude.** J'avais écrit que `GUID_OPTIONS`
bit 3 donnait « l'espace d'action attitude + poussée, le même que Betaflight en ACRO ». Faux :
**ACRO est du rate**, et le bit 3 ne change que la sémantique de la poussée, jamais celle de
l'attitude — c'est le `type_mask` qui décide angle vs rate. Le mode 20 expose en fait **trois
barreaux** : (1) quaternion + bit 0 = attitude + alt-hold baro ; (2) quaternion + bit 1 =
attitude + poussée ; (3) `ATTITUDE_IGNORE` + les 3 rates + bit 1 = **CTBR**, l'espace des papiers
de RL. Preuve au niveau de l'appel (`mode_guided.cpp:1025-1036`) : `attitude_quat.is_zero()` →
`input_rate_bf_roll_pitch_yaw_rads()`, sinon `input_quaternion()`.

Conséquence d'architecture, actée : **typer `VehicleBackend` au niveau CTBR** (plus petit
dénominateur commun) et faire de l'attitude une couche de confort au-dessus. Typée en attitude,
l'interface serait inadaptée à ACRO et une politique CTBR ne pourrait pas s'y brancher.

**3. Piège découvert au passage — le timeout du contrôleur d'angle.**
`angle_control_run()` (`mode_guided.cpp:983-993`) : sans mise à jour pendant `GUID_TIMEOUT`
(`MAX(g2.guided_timeout, 0.1) × 1000` ms), il remet l'attitude à plat au cap courant, annule les
rates **et force `use_thrust = false`**. Une politique CTBR dont la boucle bégaie **retombe
silencieusement du barreau 3 au barreau 1** — deux régimes de contrôle, aucun message. Filet de
sécurité utile, mais à régler bas (0,2-0,5 s) et à logger.

**Autres trous du build minimisé, repérés sur le chemin critique :**
`HAL_GYROFFT_ENABLED 0` → **`INS_HNTCH_MODE=4` (FFT en vol) n'existe pas**, ce qui touche
directement l'étape 7 de `plan_correction_poussee.md` : il reste `MODE=1` (piloté par les gaz)
ou `MODE=3` (télémétrie ESC, peu probable avec un UART RX-only et du BLHeli_S stock).
`MODE_SYSTEMID_ENABLED 0` → pas de balayage fréquentiel (optionnel, mais c'est ce qui produirait
une vraie identification chiffrée plutôt qu'un Autotune boîte noire).
`AP_RC_CHANNEL_AUX_FUNCTION_STRINGS_ENABLED 0` → pas de chaîne de confirmation en clair sur les
`RCx_OPTION` ; ne pas lire ce silence comme un échec.

**Piège de terrain :** `ModeGuidedNoGPS::requires_position()` renvoie `false` (`mode.h:1278`),
donc pas de blocage EKF — mais il hérite de `ModeGuided::allows_arming()` =
`option_is_enabled(AllowArmingFromTX)` = **`GUID_OPTIONS` bit 0** (`mode_guided.cpp:126`). Donc
armer en Stabilize puis basculer (le plus propre), ou poser le bit 0. Et le mode doit être
atteignable : `FLTMODE` occupés par 0/2/9 → basculer par `SET_MODE` depuis le companion.

**Lien de commande whoop — inconnue levée :** la Pocket expose un **DSC 3,5 mm TRS** *et* une
**baie nano 8 broches**. Voie à 0 € (ESP32-C3 déjà en stock → DSC → EdgeTX Trainer → ELRS interne)
à tester en premier ; plafond **~45 Hz en PPM**, suffisant pour les barreaux 1-2, limitant pour du
CTBR. La baie nano (~25 €, CRSF 250-500 Hz) devient un achat justifié par une mesure.

## 2026-07-27 — Correction de la plage de poussée : le drone devient pilotable, et une 2e cause apparaît

Journée d'itération rapide (5 vols, params + log relus à chaque fois). Le plan du 25/07 a été
appliqué ; il a marché sur ce qu'il visait, et il a démasqué un problème indépendant.

### Ce qui a été fait

1. **MP → Initial Tune Parameters**, hélice **3,5"** (champ « Aircrew size in inch » = *Airscrew*,
   coquille de MP). Piège rencontré : MP tourne en locale FR, `ConvertToDouble` plante sur
   « 3.5 » → **il faut taper « 3,5 » avec une virgule**. Ce bloc a posé les FILTRES
   (`INS_GYRO_FILTER` 20→101, `ATC_RAT_*_FLTD/FLTT` 20→50,5, `INS_ACCEL_FILTER` 20→10),
   `MOT_THST_EXPO` 0,65→0,43, la compensation de tension (`MOT_BAT_VOLT_MAX/MIN` 16,8/13,2)
   et les seuils batterie. **Il n'a PAS touché aux gains PID** — point important pour la suite.
2. **Calibration `MOT_SPIN_ARM` = 0,03 / `MOT_SPIN_MIN` = 0,05** (les moteurs démarrent dès 1 %
   au banc, mais on choisit la fiabilité : le BLHeli_S commute mal à très bas régime, et la
   sensibilité de la poussée de hover entre 0,02 et 0,06 est négligeable — c'est le plafond
   `MOT_SPIN_MAX` qui ferait le gros du travail, pas le plancher).
3. `MOT_THST_HOVER` réglé sur la valeur **mesurée** (0,179).

### Résultat : la saturation du mélangeur est bien la cause du gros wobble

Comparaison sur hover stabilisé (log 14-07-40 du 24/07 vs 14-20-00 du 27/07) :

| | avant | après |
|---|---|---|
| erreur d'assiette roulis (std / moyenne) | 5,13° / **+5,96°** | **0,82° / +0,67°** |
| erreur tangage | 3,03° / +8,03° | **0,46° / +1,87°** |
| poussée de hover | 0,052 | **0,178** (> plancher dur 0,125) |
| manche des gaz au hover | 6 % | **46 %** |
| gaz demandés → appliqués | ×2,4 | **×1,0** |
| intégrateur tangage | 0,0007 (gelé) | 0,0035 (il travaille) |
| mélangeur saturé | 99,4 % | 78,5 % |

Victor : « première fois que je peux le faire voler stable dans ma chambre, c'était impossible
avant ». Le shaking lent a disparu, remplacé par une vibration rapide. **`MOT_SPIN_MAX` n'a
finalement PAS été plafonné** : l'objectif (sortir du plancher 0,125) était atteint, et la
saturation résiduelle venait d'ailleurs (voir plus bas).

### Incident : montée au plafond → `MOT_THST_HOVER` écrit à 0,6864

Entre deux vols, `MOT_THST_HOVER` s'est retrouvé à **0,6864** = le maximum du firmware
(`AP_MOTORS_THST_HOVER_MAX` = 0,6875), soit **3,9× le hover réel**. Preuve que ce n'est pas un
apprentissage : `CTUN.ThH` était **plat à 0,2000 pendant tout le vol précédent** et déjà à
0,6864 au boot du suivant → écrit au clavier. Suspect n°1 : le séparateur décimal encore
(« 0.18 » avalé comme « 18 », puis écrêté et sauvegardé par le firmware).

Double effet, tous les deux vers le haut : la **courbe de manche** devient ~1,5× plus raide
(hover à 10 % de manche au lieu de 46 %) et le **plancher du mélangeur** repasse au-dessus de
la commande (`ThO 0,113 → ThrOut 0,165`, ×1,46). En chambre → plafond.
**Leçon : relire toute valeur écrite dans MP sur cette machine.**

Le refus d'armement qui a suivi n'était pas une panne : `PreArm: Battery 1 below minimum
arming voltage`, `BATT_ARM_VOLT` = 14,7 posé par Initial Tune. Idem l'atterrissage
automatique du vol suivant : `Battery 1 is low 14.27V used 65 mAh` → Battery Failsafe → RTL et
Smart RTL refusés (pas de position en intérieur) → repli sur LAND (`BATT_FS_LOW_ACT`=3).
**Les packs sont fatigués : 14,27 V après 65 mAh sur un 850 mAh.** Devenu le facteur limitant
(vols de 20 s = fenêtres d'analyse trop courtes).

### La FFT enfin propre : 15,5 Hz, et ce n'est ni le bruit moteur ni le tune

`INS_LOG_BAT_MASK=1` + `INS_LOG_BAT_OPT=1` (**bit 0 = cadence CAPTEUR ~989 Hz**, pas la cadence
de boucle : à 400 Hz, Nyquist 200 Hz, on aurait réaliasé la fondamentale moteur à ~250 Hz).
Deux corrections à mes conseils antérieurs, vérifiées dans le source : retirer `NTUN` ne
supprime pas les messages EKF (il ne gate que `PSCD`), et `ATTITUDE_FAST` **augmente** le log
EKF (10 → 25 Hz).

| | gyro | accéléro |
|---|---|---|
| roulis (X) | rms 1,15 rad/s, **96 % dans 15-30 Hz, pic 15,5 Hz** | 48 % dans 120-250 Hz, pics **238-268 Hz** |
| tangage (Y) | rms 0,23 | idem bande moteur |
| lacet (Z) | rms 0,29 | idem |

Conséquences :
- La fondamentale hélice (238-268 Hz) sature l'accéléro mais **le gyro n'en voit que 0,2 %** :
  `INS_GYRO_FILTER=101 Hz` fait son travail. → **Le notch harmonique ne sert à rien ici**,
  étape rayée du plan.
- Les 15,5 Hz sont **dans le gyro et quasi absents de l'accéléro** (0,5 % en 15-30 Hz) →
  rotation pure, sans translation.
- **Spécificité roulis : gyro X 1,15 contre Y 0,23 et Z 0,29 (×5).**

### Erreur d'analyse, puis réfutation (2 itérations)

Après avoir divisé le D par 2 (0,0036→0,0018), le gyro roulis a baissé de 25 % et j'en ai
conclu « fréquence figée + amplitude qui suit le gain = c'est la boucle ». **C'était faux** :
ce −25 % était un artefact (autre batterie, fenêtre de 20 s). Le vol suivant l'a réfuté.

| | D=0,0036 P=0,135 | D=0,0018 P=0,135 | D=0,0009 P=0,09 |
|---|---|---|---|
| gain de boucle à 15,5 Hz | 0,486 | 0,310 | **0,178** (−63 %) |
| terme D (sortie PID) | 0,3768 | 0,2619 | **0,0740** (÷5) |
| écart moteur instantané | 474 µs | 394 µs | **99 µs** (÷4,8) |
| **gyro roulis rms en vol** | 1,293 | 1,184 | **1,416** |
| **gyro roulis max** | 1,749 | 1,647 | **1,751** |
| **fréquence** | 15,6 Hz | 16,4 Hz | **15,5 Hz** |
| erreur d'assiette roulis | 1,43° | 1,18° | 1,31° |

**La commande moteur a été divisée par 5 sans le moindre effet sur le gyro.** Ce n'est pas la
boucle. Une fréquence insensible à un facteur 3 sur le D et 1,5 sur le P est une **résonance
mécanique**. Les gains actuels (P=I=0,09, D=0,0009) sont bons et gardés : même erreur
d'assiette qu'à l'origine pour une commande 5× plus douce.

### Ce n'est pas le gyro non plus

Test tiré du même log — amplitude de la raie 13-19 Hz du gyro roulis selon le régime :

| moteurs | amplitude |
|---|---|
| **arrêtés (désarmé)** | **0,013 rad/s** |
| ralenti | 0,0007 |
| proche hover | **1,292** (×100) |

Absente moteurs coupés → **capteur sain, excitation mécanique réelle**.

### État et prochaine étape

Deux candidats que le log ne départage pas — j'avais affirmé « c'est la FC », Victor a
objecté à raison qu'il avait vérifié la visserie avant/après les vols extérieurs, donc **la
résonance existait AVEC les écrous en place** (ils ont été retrouvés manquants après le
dernier vol : conséquence plausible, pas cause) :
1. **la FC sur ses silentblocs** ;
2. **la batterie sur sa sangle** — montée en longueur sur le dessus, ~1/3 de la masse, libre de
   basculer latéralement = axe de roulis, ce qui collerait avec la spécificité roulis.

Correction de raisonnement à noter : mon argument « les moteurs bougent 5× moins donc la
cellule ne peut pas rouler, donc c'est la carte » est incomplet. Il élimine un roulis *piloté
par les hélices*, mais **une masse interne qui oscille applique un couple de réaction sans
aucune commande moteur** et produit la même signature.

**Prochain test : essai modal, sans voler.** `LOG_DISARMED=1`, drone sous tension sur la table,
hélices démontées, pichenette sèche successivement sur la batterie, la stack, puis chaque
bras, 5 s entre chaque. Chaque structure sonne à sa fréquence propre → on lit le spectre
autour de chaque impact et on identifie celle qui résonne à 15,5 Hz. Gratuit, sans risque, et
ça ne consomme pas de batterie (devenue le facteur limitant).

Ensuite seulement : Autotune — surtout pas avant, il calerait ses gains sur un gyro qui ment.

**Leçons de méthode.** (1) Vérifier la cadence d'échantillonnage avant de croire une
fréquence : le « 0,7 Hz » de départ était un repliement d'ATT loggé à 10 Hz, la vraie valeur
est 15,5 Hz — et mon estimation par calcul de repliement sur le log du 24/07 donnait 15,4 Hz,
donc la résonance est bien là depuis le début. (2) Un delta de 25 % sur une seule mesure, avec
deux conditions expérimentales différentes, ne prouve rien : il fallait le troisième point
pour réfuter. (3) Faire varier le gain d'un facteur 3 et regarder si la fréquence bouge est le
test le plus discriminant entre « boucle » et « mécanique ».

## 2026-07-28 — La résonance est structurelle : trois invariances, et l'essai modal qui rate

Suite du 27/07. Objectif : départager « FC sur silentblocs » vs « batterie sur sangle ».

### L'essai modal (pichenettes) : raté, et pourquoi

Deux échecs d'instrumentation avant même d'avoir des données :

1. **`LOG_DISARMED` mis à 1 *après* l'effacement de la puce, sans reboot.** Sur le backend
   `AP_Logger_Block` (puce SPI), un nouveau log ne s'ouvre que sur `new_log_pending` —
   c'est-à-dire à l'armement, après un effacement, ou au boot. Basculer le paramètre en cours
   de session ne rouvre rien. Le drone n'ayant jamais été armé, **zéro octet écrit**.
   → `LOG_DISARMED` exige un **redémarrage de la FC** pour prendre effet ici.
2. **« Chip full, logging stopped » puis « PreArm: Logging failed ».** Avec `LOG_DISARMED=1`
   *et* l'échantillonneur par lots à 989 Hz, la FC écrit ~27 ko/s **en permanence dès la mise
   sous tension**, désarmée : les 8 Mo se remplissent en **~5 minutes**, avant même le début du
   test. Parade : `LOG_BITMASK=136954` (sans GPS/compas/caméra/optflow/CMD) → ~7 ko/s, soit
   ~20 min d'autonomie de puce. Et le nouveau log apparaît daté **1970/1980** (pas de pile
   RTC) : prendre le **numéro le plus élevé**, jamais la date.

Le log finalement obtenu (437 s, désarmé) ne montre **aucun impact exploitable** : le sampler
n'écoute que **45 % du temps** (189 lots de 1,04 s, ~1,2 s de trou entre chaque) et l'énergie
des pichenettes est **~100× sous** l'oscillation en vol (rms max 0,0155 rad/s contre 1,3 en
vol). Les 3 seuls événements notables résonnent à 8,7 / 32,8 / 96,6 Hz — pas de 15,5 Hz.
**Victor avait exprimé des doutes sur ce test : ils étaient fondés.**

### Ce qui a tranché : les invariances, sur les logs déjà en main

Plutôt que de renvoyer au banc, exploitation des logs existants.

| on fait varier | facteur | fréquence de la raie gyro |
|---|---|---|
| gain de boucle (P, D) | ÷3 sur D, ÷1,5 sur P | **inchangée** |
| régime moteur (fondamentale accéléro 235→256 Hz) | ±4 % | **inchangée, ±1,5 %** |
| vols successifs (3 jours, params différents) | — | 15,44 / 15,83 / 16,22 Hz |

La fondamentale hélice balaie 235-256 Hz pendant que la raie gyro reste à 15,45-15,93 Hz.
→ **résonance structurelle à fréquence fixe (~15,7 Hz), excitée large bande par les rotors.**
Élimine définitivement : la boucle de commande, le gyro (raie absente moteurs coupés, ×100
entre arrêt et hover), le balourd et le battement entre moteurs (les deux suivraient le régime).

**Indice sur le coupable** : dans le dernier vol — celui après lequel les écrous de la stack
ont été trouvés manquants — la fréquence est de **15,44 Hz avec un écart-type de 0,00 Hz**
(tous les lots dans le même bin FFT), et n'a pas bougé de plus de 5 % vs les vols précédents.
Perdre deux écrous ramollit fortement le montage : si la stack était la masse résonante, la
fréquence aurait chuté bien davantage. **Oriente vers la batterie plutôt que vers la FC** —
mais l'instant exact de la perte des écrous est inconnu, donc non conclusif.

### Prochaine étape : test différentiel, 2 vols de 30 s

Principe : ne pas chercher à *réparer*, mais à **changer la raideur d'un élément et voir si la
fréquence se déplace**. Une résonance qui bouge quand on rigidifie X prouve que X est dedans.

- **Vol A** : remonter les écrous de la stack (nécessaire de toute façon) → hover 30 s.
- **Vol B** : bloquer la batterie (sangle serrée + cale de mousse dense ou double-face) → 30 s.

Un seul changement par vol ; métrique = fréquence + amplitude de la raie 13-19 Hz du gyro X.

**Enjeu** : 1,28 rad/s rms à 15,5 Hz = oscillation d'assiette de **±0,75°**. Le drone vole
correctement (erreur d'assiette 1,3°, commande moteur ÷5, maniable), donc ce n'est plus
bloquant pour le vol — mais ça l'est pour la suite : **l'Autotune se calerait sur un gyro
pollué**, et surtout c'est **une vibration à 15 Hz sur la caméra**, donc sur la chaîne de
perception qui est le cœur d'ARGOS.

**Leçon de méthode** : quand un test dédié échoue, regarder d'abord ce que les données déjà
collectées peuvent dire. Les trois invariances étaient dans les logs depuis le 27/07 — faire
varier une grandeur et vérifier si la fréquence suit est plus discriminant qu'un essai modal
mal instrumenté.

## 2026-07-28 (clôture) — Pause hardware forcée, bascule sur SITL

**Blocage mécanique** : les pas de vis de la plaque supérieure du cadre sont morts → pas d'accès
à la stack → impossible de remonter la visserie de la FC, de remplacer les silentblocs par des
entretoises rigides, ni de faire le test de masse ajoutée. **C'est le chemin critique**, et
aucun vol supplémentaire ne peut apporter d'information tant qu'il n'est pas levé. Vols de
diagnostic suspendus.

### État du drone à la mise en pause

| | 24/07 | à la pause |
|---|---|---|
| erreur d'assiette roulis (std) | 5,13° | **1,31°** |
| dérive | permanente, direction variable | **supprimée** (intégrateurs libérés) |
| manche des gaz au hover | 6 % de course | **47 %** |
| AltHold | structurellement impossible | **débloqué** |
| commande moteur | butée 1050↔1550 | **÷5** |
| poussée de hover | 0,052 | 0,173 (`MOT_THST_HOVER`=0,179, ×1,0) |

**Reste une seule anomalie** : résonance mécanique **15,44 Hz** (σ=0,00), axe de roulis,
amplitude ±0,75° d'assiette. Éliminés par la mesure : boucle de commande (gain ÷3 sans effet),
gyro (raie absente moteurs coupés, ×100 entre arrêt et hover), balourd et battement moteurs
(fréquence insensible à un balayage 235→256 Hz de la fondamentale), **batterie** (fréquence
inchangée alors que la rotation à 90° change massivement l'inertie de roulis), câbles/RX/VTX
(argument de moment cinétique : un câble de 3 g à 5 cm oscillant de ±2 mm à 15 Hz manque d'un
facteur ~50 pour produire 75 °/s sur 300 g).

**Suspect restant : la stack sur son montage souple.** Argument décisif — une pièce qui *porte
l'IMU* n'a besoin de faire tourner qu'elle-même de ±0,75° pour saturer le gyro, alors que
n'importe quelle autre masse devrait secouer tout le drone. Cohérent avec 96 % de l'énergie
dans le gyro contre ~1 % dans l'accéléro, et avec les écrous de stack retrouvés manquants.
**Test à faire dès l'accès rétabli** : scotcher quelques grammes sur la stack → si la fréquence
descend, c'est elle (même logique que celle qui a éliminé la batterie).

Note : l'orientation batterie **en travers** fait tomber la résonance de 100 % à **5 % du
temps** (amplitude ÷10) sans déplacer la fréquence — donc ce n'est pas la cause, mais c'est un
état de vol exploitable en attendant.

**Reliquats à traiter à la reprise** : `AHRS_TRIM` toujours à zéro (le `SAVE_TRIM` posé sur
RC7_OPTION=5 n'a jamais pu se déclencher — il exige les gaz à zéro, donc inutilisable en vol ;
utiliser **`RC7_OPTION=182`** `AHRS_AUTO_TRIM`, vérifié compilé dans le binaire) ; CG encore
reculé de 34 µs d'écart moteur (avancer la batterie de 5-10 mm, cible < 10 µs) ;
`LOG_BITMASK=136954` avant tout nouveau vol de diagnostic.

### Suite : SITL, barreaux 1 et 2 de l'échelle GPS-denied

Le chapitre flow (`docs/ekf_flow_fusion.md`) a ses deux premiers barreaux réalisables **sans
aucun matériel** : (1) flow natif SITL — valider la fusion `EK3_SRC1_VELXY=5`, FlowHold, et la
lecture des innovations ; (2) SITL + script d'injection `OPTICAL_FLOW` en MAVLink2 — c'est
exactement l'interface qu'utilisera le Pi, et c'est là que se maîtrisent cadence d'envoi,
timestamps et gestion de `quality`. Barreau 3 (caméra Gazebo + vrai algo) accessible aussi,
l'infra Gazebo tourne déjà.

**Action parallèle** : commander la liste §9 du doc flow (~110 €, TFmini-S + Arducam OV9281 +
Pi Zero 2W) en même temps que les pièces de cadre, pour que la pause serve à quelque chose.

## 2026-07-29 — Le modèle est renversé par le test du notch, puis arrêt du hardware

Journée de mesures fines (spectres d'amplitude, log à 400 Hz, notch), qui a d'abord **confirmé**
ma conclusion de la veille puis l'a **réfutée**. Détail complet et autoportant désormais dans
**`docs/diagnostic_complet.md`** — le journal ne garde que le fil et les corrections.

### Victor teste ma principale hypothèse, et elle tombe

Il scotche fermement la FC vers le bas (raideur ≈ celle des boulons) → **aucune différence en
vol**. Ma piste « il manque des écrous sur la stack » ne tient pas. Il objecte aussi, à raison,
que la visserie était en place lors des premiers vols et que la résonance y était déjà.

### La mesure qui manquait : des spectres d'AMPLITUDE, pas des rms

J'avais raisonné jusque-là sur des rms par bande. En sortant les vraies amplitudes de sinusoïde
sur le gyro brut (989 Hz) :

```
GYRO  à 15,47 Hz : X = 101,0 deg/s   Y = 2,06   Z = 14,3    -> X/Y = 49
ACCEL à 15,47 Hz : X = 0,037 m/s2    Y = 0,577  Z = 0,231
```

Ce n'est pas « à dominante roulis », c'est un **roulis pur**. Amplitude angulaire **±1,04°**.

**Deux calculs neufs, jamais faits jusque-là :**

1. **Localisation de l'axe.** `|a_y| = θ·|g − r·ω²|` → l'axe passe à **~4 mm de la puce IMU**
   (2,3 et 4,8 mm sur deux vols indépendants). Si c'était la cellule qui roulait, l'axe passerait
   par son CG, 10-25 mm plus haut, et l'accéléro verrait 1,9-4,5 m/s² au lieu de 0,577.
2. **Argument énergétique.** Faire rouler la cellule de ±1,04° à 15,4 Hz demanderait
   180 rad/s² → 0,072 N·m → **61 g de différentiel PAR MOTEUR à 15 Hz**, sur un hover de 70 g
   par moteur, avec une constante de temps moteur de 10-20 ms. **Impossible.**

Même raisonnement pour les câbles/RX/VTX : un câble de 3 g à 5 cm oscillant de ±2 mm à 15 Hz
manque d'un **facteur ~50** en moment cinétique. En revanche une pièce qui *porte l'IMU* n'a
besoin de faire tourner qu'elle-même — d'où 96 % de l'énergie dans le gyro contre ~1 % dans
l'accéléro.

→ Conclusion posée : **c'est la carte/stack sur son montage souple, pas la cellule.**

### Le trou de raisonnement, bouché : log à 400 Hz

`RATE`/`PIDR` étaient loggés à 10 Hz — je n'avais **jamais pu voir** si la FC commandait du
roulis à 15,4 Hz. Avec `LOG_BITMASK` bit 0 (`ATTITUDE_FAST`), 267-400 Hz :

```
prédiction depuis P=0,09 / D=0,0009 :  ROut = -(P + jωD)·R  ->  0,0578, phase -136°
mesuré :                                                        0,0633, phase -151°
```

À 10 % près, la sortie de la FC est **exactement la réponse linéaire de ses PID**, et sa phase
est majoritairement **opposée** au mouvement (composante en phase −0,87 = amortissante).
J'en ai conclu — trop vite — que la boucle était innocentée.

### Le notch renverse tout

Notch fixe `MODE=0, FREQ=15, BW=8, ATT=30` :

```
                                     avant        avec notch
RATE.R   à 15,4 Hz (vu du contrôleur)  26,4 deg/s -> 3,13
ROut     à 15,4 Hz (commande)           0,0633    -> 0,0063
GYRO BRUT à 15,4 Hz (physique)        101,0 deg/s -> 1,6      <- !!
nouveau pic (gyro brut)                    —      -> 7,72 Hz = 340,7 deg/s
```

Vérifié dans le source que l'échantillonneur reçoit bien `raw_gyro` (le driver Invensensev3
n'active pas le chemin « sensor rate », donc passage par `log_gyro_raw()` avec
`!doing_post_filter_logging() ? raw_gyro : ...`). **Le notch n'agit que sur ce que voit le
contrôleur, et pourtant la raie a disparu du mouvement physique.** Une résonance purement
externe serait restée.

→ **La boucle participe. Ma conclusion « purement mécanique » était fausse.**

Effet secondaire : phase mangée dans la bande de la boucle → **instabilité à 8 Hz, amplitude
3× plus grande** (le pilote la décrit comme « lente et beaucoup plus ample, pire en
maniabilité »). Notch annulé.

### Modèle retenu : couplage mode mécanique ↔ boucle

```
la carte bascule sur ses silentblocs (mode propre ~15,4 Hz)
 -> le gyro boulonné dessus le rapporte comme une rotation de l'appareil
    -> la FC commande les moteurs -> la cellule bouge
       -> le mouvement se réinjecte dans le montage -> entretient le basculement
```

Il explique enfin **toutes** les observations qui se contredisaient : fréquence figée malgré
÷3 sur D (fixée par le mode mécanique), amplitude peu sensible au gain (cycle limite), absence
moteurs coupés, insensibilité au régime, axe à 4 mm de l'IMU, et surtout **l'essai « batterie
en travers » (amplitude ÷10, fréquence inchangée, intermittent)** = gain de boucle passé
marginalement sous 1. Ce n'était pas une anomalie inexplicable.

**⚠ Contradiction non résolue** : le §5.7 (commande amortissante, purement linéaire) et le
§5.6 (le notch tue la raie physique) ne se concilient pas proprement. Possible que lors de la
mesure « avec notch » le drone ait été dans un état si différent (8 Hz, 340 deg/s) que le mode
à 15,4 Hz n'était plus excité. **Consigné comme incertitude n°1 du diagnostic.**

### Arrêt du hardware

Notch adouci (`BW=4, ATT=15`) essayé : « rien changé » d'après le pilote, **non vérifié** —
téléchargement à 0 octet. Cause identifiée : les deux logs précédents faisaient **8,1 Mo**
chacun (puce pleine à chaque session à cause de l'échantillonneur), et tirer 8 Mo sur MAVLink
échoue systématiquement.

Décision : **on arrête les itérations logicielles.** Rendements décroissants — le mécanisme est
compris, les deux remèdes (montage rigide, ou notch correctement dosé) demandent soit l'accès
au cadre soit un réglage qui déstabilise à chaque essai, et chaque tentative coûte une
batterie + un téléchargement raté. Retour au jeu de paramètres connu bon :

```
INS_HNTCH_ENABLE = 0    LOG_BITMASK = 136954    INS_LOG_BAT_MASK = 0
```

Le drone reste dans un état correct : vole, erreur d'assiette 1,31°, maniable, dérive
supprimée. Reste une vibration comprise et non dangereuse.

### Livrable

**`docs/diagnostic_complet.md`** — diagnostic autoportant (487 lignes) : matériel, piège de
l'échantillonnage, les deux problèmes avec mécanismes vérifiés dans le source, tableau des
hypothèses éliminées **avec la mesure qui les élimine**, jeu de paramètres commenté, questions
formulées pour un forum, et **§7 : neuf points d'incertitude explicites**. `diagnostic_wobble.md`
marqué comme historique.

À faire à la reprise, par ordre : extraire les vis foirées de la plaque supérieure → entretoises
M3 rigides → vol de 30 s → si la raie disparaît, dossier clos ; sinon test de masse ajoutée.
Et **peser le drone** : l'inertie de roulis du §5.3 est calculée depuis une masse jamais mesurée.

**Leçons.** (1) Un rms par bande n'est pas une amplitude : les deux calculs décisifs (axe de
rotation, énergie nécessaire) n'ont été possibles qu'en passant à des spectres d'amplitude.
(2) Vérifier *où* un signal est prélevé dans le code avant d'interpréter sa disparition — c'est
ce qui rend le test du notch concluant. (3) Une conclusion tenue deux jours peut tomber sur une
seule mesure : il faut la chercher activement, pas attendre qu'elle arrive.

## 2026-07-29 (soir) — Poids latéraux : la fréquence ne bouge pas, l'intermittence se mesure enfin

Victor colle quelques grammes **sur les côtés de la batterie** (pour reproduire l'effet du
montage en travers) et rapporte un vol nettement plus lisse. Trois vols analysés.

### Une conclusion posée puis retirée dans l'heure

Le premier log exploitable montrait la raie à **9,83 Hz avec rms 179 deg/s** au lieu de
15,6 Hz / 76 deg/s. J'en ai conclu que la masse ajoutée **déplaçait la fréquence** (`f ∝ √(k/m)`),
donc que **la batterie faisait partie du résonateur** — et j'ai annoncé que ça contredisait le
diagnostic.

**C'était faux.** Le vol suivant, **mêmes poids en place et instrumentation complète**
(`LOG_BITMASK=136955` + échantillonneur), donne le gyro brut lot par lot :

```
15.50  15.50  15.49  15.49  15.49  15.48  15.48  15.48  15.48  15.47  15.47 Hz
```

**Fréquence rigoureusement inchangée.** L'épisode à 9,8 Hz était un **phénomène distinct**
(oscillation lente et ample, plausiblement induite par le pilote), pas un décalage de la raie.
La conclusion d'origine tient : ajouter de la masse latérale ne déplace pas la fréquence.

### Ce que ce vol apporte vraiment : l'intermittence, quantifiée

À **fréquence constante**, l'amplitude varie d'un facteur **30** dans un même vol :

```
t=33,7s   9,2 deg/s    <- calme
t=36,2s   9,0
t=38,3s  79,0          <- ça s'accroche
t=40,5s  74,6
t=42,9s  21,7
t=49,8s   2,8          <- très calme
t=58,9s  52,1          <- reprend juste avant l'atterrissage
```

Correspond exactement au récit du pilote, dernières secondes comprises. **Aucune corrélation
propre trouvée avec la position ou la vitesse des gaz** : son hypothèse « ça ne vibre que quand
le régime est stable » n'est ni confirmée ni infirmée.

Conséquence méthodologique majeure, désormais écrite dans le diagnostic : **sur ce système,
une mesure isolée ne prouve rien.** Toute comparaison avant/après doit porter sur plusieurs
dizaines de secondes et sur la *fraction de temps en résonance*, jamais sur un seul lot FFT ni
sur une impression ponctuelle.

### Mises à jour de `docs/diagnostic_complet.md`

- **§5.4** : colonne « solidité » ajoutée au tableau des invariances. Le test « masse ajoutée
  latéralement » (11 lots, forte) remplace comme preuve principale le test « batterie en
  travers » (**un seul lot, faible**).
- **§5.4bis** (nouveau) : l'intermittence, avec les chiffres et la consigne de méthode.
- **§7** : encadré d'avertissement en tête, listant **les trois conclusions que j'ai dû
  retirer** au cours de l'enquête (« c'est le tune », « c'est purement mécanique, la boucle est
  innocentée », « la masse ajoutée déplace la fréquence ») et leur cause commune : conclure sur
  une mesure unique, alors que l'amplitude varie spontanément d'un facteur 30. Deux nouveaux
  points d'incertitude (§7.2 l'élimination mal étayée de la batterie, §7.3 l'épisode à 9,8 Hz).
  Liste portée à **11 incertitudes explicites**.

### État

Rien ne change côté paramètres ni côté plan : `INS_HNTCH_ENABLE=0`, gains inchangés, blocage
toujours sur l'accès à la stack. Les poids latéraux semblent augmenter la fraction de temps
calme — **non démontré**, faute d'un vol de référence dans les mêmes conditions.

**Leçon.** Ce système punit sévèrement l'inférence sur échantillon unique. Trois de mes
conclusions sont tombées pour cette raison exacte. La parade est écrite dans le diagnostic :
comparer des fractions de temps sur des dizaines de secondes, pas des pics isolés.

## 2026-07-29 (nuit) — RÉSOLU : la résonance était un cycle limite entretenu par la boucle

Test des gains réduits (`ATC_RAT_RLL/PIT_P = I = 0,06`, `D = 0,0005`) évalué sur la **bonne
métrique** — le pourcentage de temps en résonance, pas l'amplitude des pics. Résultat sans
ambiguïté.

| | avant (P=0,09 / D=0,0009) | **après (P=0,06 / D=0,0005)** |
|---|---|---|
| lots gyro bruts | 16 (36 s de vol) | **21 (51 s)** |
| amplitude de la raie, moyenne | 43,2 deg/s | **1,7** |
| amplitude, max | 109,4 deg/s | **3,5** |
| **temps en résonance (>40 deg/s)** | **44 %** | **0 %** (0/21) |
| rms `RATE.R` en vol | 50,2 deg/s | **10,6** |

Pilote : « aucun shaking, ultra smooth de A à Z ». Amplitude ÷25 sur **plus** de données que la
référence — ce n'est pas une fenêtre calme par chance.

**Aucune contrepartie, tout s'améliore simultanément :**

```
erreur assiette roulis  : std 0,71° -> 0,40°   moy -0,31° -> -0,02°   max 3,0° -> 1,0°
erreur assiette tangage : std 2,40° -> 1,76°
mélangeur saturé        : 11 %  ->  0 %
écart instantané moteurs: 276 us -> 126 us     (p95 578 -> 153)
```

Le suivi d'attitude est **meilleur** avec des gains plus faibles : l'oscillation consommait
l'autorité de commande, la supprimer a libéré la boucle.

### Conclusion sur le mécanisme

Le modèle du couplage mode mécanique ↔ boucle (entrée du 29/07) est **confirmé**, et le côté
« boucle » est le levier actionnable : le gain de boucle à 15,5 Hz était au-dessus du seuil
d'auto-entretien, une baisse d'un tiers l'a fait passer dessous.

Ça explique rétrospectivement mes fausses pistes : les deux baisses de gains précédentes
(0,135→0,09 puis D÷4) **réduisaient le gain sans franchir le seuil**, et je mesurais
l'*amplitude* — laquelle, dans un cycle limite, est fixée par la non-linéarité et pas par le
gain. D'où « aucun effet » alors qu'on s'approchait du seuil. À P=0,09 le système était pile
dessus, d'où l'intermittence à 44 %.

**Et l'ordre des opérations était contraint** : au départ, baisser les gains n'aurait rien
donné du tout, puisque la saturation du mélangeur les annulait mathématiquement
(`rpy_scale = -throttle_avg_max / rpy_low`, cf. §4.3 du diagnostic). Il fallait réparer la
plage de poussée pour que les gains **existent**, puis les régler.

### Réserves

- **Le mode mécanique à 15,5 Hz existe toujours**, on a seulement cessé de l'exciter. On est
  sous le seuil, mais pas loin.
- **Ne pas lancer l'Autotune tel quel** : son principe est de monter les gains jusqu'à la
  limite — il retrouverait ce cycle limite et calerait dessus. Soit rigidifier d'abord le
  montage de la stack, soit `AUTOTUNE_AGGR` bas avec le doigt sur l'interrupteur.
- Un seul vol (51 s, 4 segments). À reconfirmer, idéalement en extérieur.
- Les **poids latéraux** sur la batterie sont désormais une variable inutile à tester : les
  retirer et refaire 60 s pour voir si le 0 % tient.

### Jeu de paramètres qui marche

```
MOT_SPIN_ARM 0,03   MOT_SPIN_MIN 0,05   MOT_THST_EXPO 0,43   MOT_THST_HOVER 0,179
INS_GYRO_FILTER 101   INS_ACCEL_FILTER 10   ATC_RAT_*_FLTD/FLTT 50,5
ATC_RAT_RLL_P = I = 0,06   ATC_RAT_RLL_D = 0,0005      (idem PIT)
INS_HNTCH_ENABLE 0
```

**Leçon centrale de toute l'enquête** : choisir la bonne métrique. Pendant une semaine j'ai
évalué les changements de gains sur l'amplitude de l'oscillation — la seule grandeur qui, dans
un cycle limite, n'en dépend pas. La métrique qui a tout débloqué (fraction de temps en
résonance) n'est apparue qu'en mesurant l'intermittence.

## 2026-07-29 (nuit, suite) — Récapitulatif des deux correctifs + effet de chaque paramètre

Vol de confirmation **sans les masses latérales** sur la batterie : comportement identique,
toujours zéro shaking (rapport pilote, log non téléchargé). Les masses étaient donc bien une
variable inutile — elles servaient à tester une hypothèse abandonnée. Retirées.

### Les deux correctifs, en clair

**FIX 1 — plage de poussée.** Corrige gros wobble lent, dérive permanente, envolée AltHold,
manche des gaz utilisable sur 6 % de sa course, lacet incohérent.

```
MOT_SPIN_ARM     0.10   ->  0.03
MOT_SPIN_MIN     0.15   ->  0.05
MOT_THST_EXPO    0.65   ->  0.43
MOT_THST_HOVER   0.35   ->  0.179     <- valeur MESUREE
```

**FIX 2 — gain de boucle.** Corrige le petit shaking rapide à 15,5 Hz.

```
ATC_RAT_RLL_P = ATC_RAT_RLL_I    0.135  ->  0.06
ATC_RAT_RLL_D                    0.0036 ->  0.0005
ATC_RAT_PIT_P = ATC_RAT_PIT_I    0.135  ->  0.06
ATC_RAT_PIT_D                    0.0036 ->  0.0005
```

L'ordre est contraint : tant que le mélangeur saturait, les gains étaient annulés
mathématiquement (`rpy_scale = -throttle_avg_max / rpy_low`).

### Nouveau §0bis du diagnostic : effet de chaque paramètre

Ajouté à la demande de Victor, hors contexte vibration — pour qu'il puisse régler le feeling
sans casser ce qui marche. Points clés consignés :

- **Les gains PID ne changent pas la puissance.** Ils changent la *répartition* de la poussée
  entre moteurs, rien d'autre. La puissance vient du matériel et de la plage
  `MOT_SPIN_MIN`↔`MOT_SPIN_MAX`.
- `ATC_RAT_*_P/I` ↑ = assiette plus ferme ; ↓ = drone qui flotte. **Plafond connu sur cet
  appareil : ~0,09, au-dessus le cycle limite revient.**
- `ATC_RAT_*_D` ↑ = moins de dépassement mais amplifie le bruit gyro (moteurs chauds).
- **`ATC_ANG_*_P` (4,5, jamais touché) = le levier de toucher au manche**, et il agit à
  ~0,7 Hz donc **le monter ne réveille pas la résonance à 15,5 Hz**. C'est le réglage de
  feeling sans risque.
- **Throttle trop sensible → `MOT_SPIN_MAX`** (0,95). Le passer à ~0,70 : sortie max plafonnée
  à 1700 µs, ~25 % moins sensible, T/W de 5,6 à 4,3 (excès largement suffisant), et remonte la
  poussée de hover donc la marge de contrôle. ⚠ **`MOT_THST_HOVER` ne doit JAMAIS servir de
  réglage de feeling** — c'est une mesure, utilisée en feed-forward par le contrôleur
  d'altitude ; la fausser ramène le problème n°1 (c'est exactement ce qui a envoyé le drone au
  plafond quand elle s'est retrouvée à 0,6864). Après tout changement de `MOT_SPIN_MAX`,
  re-mesurer avec `tools/thrust_range.py`.

## 2026-07-29 (fin) — Configuration validée sauvegardée dans le dépôt

Création de **`config/`** avec les sauvegardes Mission Planner, copiées **telles quelles**
(rechargeables directement par *Load from file*, intégrité vérifiée par `diff`) :

- `argos-drone-2026-07-27-avant-corrections.param` — état d'usine, référence historique ;
- **`argos-drone-2026-07-29.param`** — ✅ configuration de vol validée, les deux correctifs.

`config/README.md` documente le contenu, la bascule mode vol ↔ mode diagnostic du logging, et
les quatre avertissements (ne jamais fausser `MOT_THST_HOVER`, plafond `ATC_RAT_*_P ≈ 0,09`,
précaution Autotune, `AHRS_TRIM` propre à cet assemblage).

Note : `AHRS_TRIM_X/Y` valent **+0,25° / +1,32°** dans le fichier final, alors que le log
analysé plus tôt donnait −1,60° / +3,75°. L'autotrim a donc été rejoué depuis, et a convergé
vers une correction plus faible. **Le fichier fait foi**, pas les valeurs du log intermédiaire.

Cette config n'existait jusqu'ici que dans la FC et sur le PC fixe — une semaine de mesures.
Elle est maintenant versionnée.

## 2026-08-04 — `GUIDED_NOGPS` en SITL : la commande passe de la vitesse à l'attitude

Réécriture de `_drone_thread` (`perception/console.py`). Avant : une consigne de **vitesse NED**
(`SET_POSITION_TARGET_LOCAL_NED`) en GUIDED — donc une boucle de position, donc du GPS.
Maintenant : **`SET_ATTITUDE_TARGET` en `GUIDED_NOGPS` (mode 20)**, `thrust = 0,5`,
cap **relatif**. Zéro estimation de position dans la commande. Les points A et B du
`PORTFOLIO.md` §1.5 ont été faits au passage, puisqu'on touchait la fonction.

### Ce qui a été construit

Un paquet `perception/control/`, trois couches qui ne se mélangent plus :

| fichier | rôle | ce qu'il n'a PAS le droit de faire |
|---|---|---|
| `guidance.py` | erreur pixel + taille de bbox -> `AttitudeCmd` | importer MAVLink, OpenCV, un simulateur |
| `gate.py` | **porte de sortie unique** : écrête, garde de proximité, émet | décider quoi que ce soit de métier |
| `vehicle.py` / `mavlink_backend.py` | traduire | décider |

`console.py` n'orchestre plus que ça. Le seul fichier du projet qui appelle `mav.*_send` pour
piloter est `mavlink_backend.py` — le jour où le backend CRSF du whoop arrive, il se pose à
côté sans toucher à la loi.

**Interface typée au niveau CTBR** (§2.3) : `send_ctbr()` est la primitive, `send_attitude()`
la couche de confort au-dessus. La loi écrite à la main consomme l'attitude ; une politique
apprise consommera la primitive. `send_ctbr()` est écrit et masqué correctement, mais **pas
encore exercé en vol** — il exige `GUID_OPTIONS` bit 3, qu'on laisse à 0.

### Les deux difficultés du §1.1, et ce qu'on a mis en face

1. **Pas de retour de vitesse → pas d'amortissement.** Un P pur sur l'erreur pixel oscille.
   Le terme **D sur l'erreur pixel EST le retour de vitesse** — c'est la vision qui le fournit,
   pas l'EKF. Filtré (passe-bas, τ = 0,25 s) et **gelé pendant un coast** : sans ça, la reprise
   après un scintillement de détection produit un pic de dérivée.
2. **Cap qui dérive → jamais de cap absolu.** `dyaw` est recalculé à partir du cap **mesuré**
   à chaque encodage, et écrêté à 5°. Effet de bord qu'on n'avait pas prévu et qui est précieux :
   le cap commandé reste par construction à quelques degrés du cap réel, **même sur un véhicule
   qui ne suit pas son lacet** (l'iris Gazebo n'a pas de couple de lacet). Le repère de
   l'attitude commandée ne peut donc pas diverger de celui du corps.

### Le piège trouvé par le test, et qui n'était trouvable qu'en volant

`sitl/nogps_engage_test.py` : cible virtuelle à position connue, caméra simulée, mais **vrai
firmware et vrai code de vol**. Premier run — le drone a **survolé la cible** sans jamais freiner.

Cause : la taille de bbox est plafonnée par la **géométrie**. Un drone à `alt` au-dessus d'une
cible de hauteur `h` ne verra jamais mieux que

    taille_max ≈ h / (2 · alt · tan(demi-champ vertical))

soit **0,147** pour une personne à 12 m. Le seuil était à **0,34** → la garde ne pouvait
littéralement **jamais** se déclencher. Aucun test unitaire ne trouve ça : la loi est correcte,
c'est le *réglage* qui est inatteignable. `size_near` n'est pas une constante, c'est une
**calibration liée à l'altitude de vol** — le test la mesure et la rapporte maintenant, et
`/tune?near=` la règle en vol.

Corrigé à `size_near = 0,12`. Deuxième run, engagement de 28,9 m :

| | |
|---|---|
| portée | 28,9 m → 14,7 m, **freine et se stabilise** |
| distance sol finale | 8,2 m, amplitude résiduelle **0,2 m** (aucune oscillation) |
| erreur horizontale | 0,40 → **0,02** |
| altitude | **11,9 – 12,0 m** sur 47 s, sans GPS dans la boucle |

Note importante sur ce résultat : **la porte n'a jamais eu à intervenir en phase 1**
(`blocked = 0`). C'est la *rampe* de la loi qui a freiné. La porte est un filet, pas le
mécanisme — si elle se déclenche en suivi normal, c'est que la loi est mal réglée.

### La porte de sortie (§1.5-A), prouvée sur firmware réel

Le défaut de conception qu'on corrige : la garde stop-when-close était un `if` **dans** la
branche de suivi ; la branche de vol manuel ne la traversait pas. **La sécurité ne couvrait que
la moitié des chemins** — l'opérateur pouvait pousser le drone dans une cible que le suivi, lui,
refusait d'approcher.

Le test a une **phase 2** dédiée : l'opérateur pousse « avancer » à fond sur une cible déjà à
distance de garde. Résultat : **105 commandes bloquées**, piqué max concédé **−0,0°**, distance
sol jamais sous 7,6 m. Rejoué en live dans la console (`/tune?near=0.06` pour descendre le seuil
sous la taille courante) : le HUD affiche `proximité (0.14)` et le piqué opérateur tombe à zéro.

C'est aussi, littéralement, la couture où ORCA / CBF-QP se branchera en swarm : une fonction qui
prend une commande désirée et en rend une sûre.

### Autres corrections faites en route

- **Un décollage raté était déclaré réussi.** L'ancien code posait `flying = True`
  inconditionnellement. Vu en vrai pendant l'intégration : `mode = GUIDED_NOGPS`, 33 commandes
  émises, `armed = false`, `alt = -0.0`. En mode 20 un `thrust = 0,5` sur un drone posé n'est
  pas neutre (`angle_control_run()` teste `land_complete`). `_takeoff()` rend maintenant un
  booléen ; s'il est faux, `flying` reste faux et **la porte refuse tout**.
- **L'armement était demandé une seule fois.** `ARMING_CHECK = 0` ne couvre pas
  `Need Position Estimate` : cette exigence vient du mode GUIDED lui-même, pas des pre-arm.
  On redemande pendant 60 s au lieu d'abandonner au premier refus.
- **Le vol manuel change de nature.** En mode 20 il n'existe pas de consigne de vitesse :
  « avancer » est un **angle de piqué**. `/fly` prend maintenant une intention normalisée
  `fwd/right/up` ∈ [−1, 1] au lieu de m/s, et repart par la même porte que le suivi.
- `GUID_TIMEOUT` posé à **0,5 s** (défaut 3 s) pour un flux à 10 Hz, et `GUID_OPTIONS = 0`
  posé explicitement — c'est lui qui décide si `thrust` est un taux de montée ou une poussée
  brute, et la console dépend du premier.

### Tests

- `perception/control/test_guidance.py` — **16 tests** au banc : symétrie de la loi, effet
  amortisseur du D, absence de pic à la reprise après coast, rampe d'approche/freinage, zone
  morte et écrêtage du cap, `dt` aberrant, écrêtage de la porte, **non-contournement de la garde
  par l'opérateur**, unitarité et réversibilité du quaternion, et la règle du tout-ou-rien sur
  les bits de `type_mask` (un mélange → `hold_position()` silencieux).
- `sitl/nogps_engage_test.py` — la preuve en SITL, 9 vérifications, verte. Demande un SITL
  **fraîchement démarré** (la cible virtuelle est posée en NED relatif à `home`).

### Reste ouvert

- Points **C** (instrumenter les liaisons) et **D** (sonde de coupure) du §1.5 : pas faits,
  c'était hors périmètre de cette session.
- **Rejouer sur la source Gazebo** : validé ici sur SITL nu (géométrie synthétique) et sur la
  source vidéo. La caméra réelle du gimbal a d'autres échelles de bbox → `size_near` sera à
  recalibrer, c'est exactement à ça que sert `/tune?near=`.
- Le barreau 3 (CTBR) est codé mais jamais émis en vol.

## 2026-08-07 — Le mode 20 volé en Gazebo : ce que le SITL n'avait pas pu montrer

Session pilotée depuis la simu 3D, avec fenêtre. Le code `GUIDED_NOGPS` de l'entrée précédente
n'avait été validé que sur SITL nu (cible virtuelle) et sur les sources vidéo. Passage sur la
source Gazebo — caméra réelle, détection réelle, opérateur réel.

### Un défaut de conception que seul le vol a montré

**L'axe d'approche n'avait aucun amortissement.** Symptôme observé en vol : le drone se cale à
la bonne distance, puis fait des allers-retours avant/arrière **de plus en plus grands** à chaque
correction, jusqu'à perdre la cible du champ. Effet boule de neige.

La cause est structurelle, pas un réglage : s'incliner commande une **accélération**, pas une
position. Un P pur sur un double intégrateur ne se stabilise pas — baisser `k_pitch` ralentit la
divergence sans jamais la supprimer.

Or la parade était déjà écrite dans la loi… **sur un seul axe** :

| axe | correction | amortissement |
|---|---|---|
| latéral | `kp_roll` | `kd_roll` (D sur l'erreur pixel) ✅ |
| approche | `k_pitch` | **rien** ❌ |

Correctif : `kd_size`, le D sur la **vitesse de grossissement de la bbox**. Même principe que
sur l'axe latéral — la caméra fournit le retour de vitesse que l'EKF ne donne pas. La dérivée
est filtrée et normalisée par la largeur de la rampe (`size_near − size_far`), pour que le gain
reste exprimé en degrés comme tous les autres.

Vérifié dans les deux sens en vol : `kdsize=10` → le drone tient la cible centrée, immobile,
longtemps. `kdsize=0` → la boule de neige revient à l'identique. Deux tests au banc figent ça,
dont un qui garantit que `kd_size = 0` reproduit exactement l'ancien calcul (donc que la
comparaison A/B est honnête). **18 tests verts.**

**Leçon de méthode :** le test SITL de l'entrée précédente approchait une cible virtuelle et
concluait « pas d'oscillation ». Il ne pouvait pas voir ce défaut — la géométrie synthétique
amenait le drone à la distance de garde par un chemin trop propre, sans jamais exciter le mode
divergent. Un banc de test vert n'est pas une preuve de stabilité.

### Réglages arrêtés en vol (profil gazebo)

`kp_roll` 7 → **4**, `kd_roll` 9 → **12** (centrage trop nerveux), `k_pitch` 5,5 → **3,5**,
`kd_size` = **10**. Réglés via `/tune` sans redémarrer, puis inscrits dans `console.py`.
`size_near` reste à 0,12 — c'est la distance de garde, encore à balayer.

### Piège d'environnement : Gazebo tournait sur le CPU

`GL_RENDERER = llvmpipe` dans `~/.gz/rendering/ogre2.log` = rendu 100 % logiciel, la RTX 4060
inutilisée. Symptôme : `shapes.sdf` fluide mais `argos_demo.sdf` saccadé (maquettes texturées +
la caméra du drone qui refait un rendu complet à 10 Hz). Corrigé par
`MESA_LOADER_DRIVER_OVERRIDE=d3d12` + `GALLIUM_DRIVER=d3d12` → `D3D12 (NVIDIA GeForce RTX 4060)`.
`run_gazebo.sh` posait déjà ces variables ; le trou était sur les lancements `gz sim` à la main.
**L'instrument de mesure à retenir :** `grep GL_RENDERER ~/.gz/rendering/ogre2.log | tail -1`.

Autre piège WSL, pour mémoire : en SSH depuis le Mac, `DISPLAY` est vide et **toute fenêtre
Gazebo meurt** (`qt.qpa.xcb: could not connect to display`). `export DISPLAY=:0` suffit, la
fenêtre s'ouvre sur l'écran du fixe. Ajouté au `.bashrc`.

### Défauts repérés en vol — et corrigés dans la foulée

Tous trouvés en volant, aucun n'était visible depuis les tests. Le correctif des trois
premiers tient dans une même idée : **`_drone_thread` ne gérait qu'un seul vol**, du
décollage à l'infini. Il gère maintenant un *cycle de vie* complet et rebouclé —
`_wait_request` → `_takeoff` → bascule mode 20 → `_fly` → détection du retour au sol →
retour à l'attente. Le fil ne meurt plus jamais, donc on redécolle sans redémarrer.

- **Le gimbal n'était commandé qu'après le décollage.** L'override RC vivait dans la boucle
  de vol, qui ne démarrait qu'une fois l'altitude atteinte → au sol et pendant la montée la
  caméra pendait librement, puis se redressait d'un coup. Extrait en `_gimbal_hold()`, appelé
  aussi depuis toutes les boucles d'attente de `_takeoff`. À noter : un override RC **expire**
  côté ArduPilot au bout de `RC_OVERRIDE_TIME` (3 s), il ne suffit donc pas de l'envoyer une
  fois — il faut le réémettre en continu.
- **La console affichait « EN VOL » sur un drone désarmé au sol.** Mesuré :
  `status="EN VOL · GUIDED_NOGPS"` avec `armed=false, alt=0.0`. Rien ne surveillait l'état réel
  après le décollage. `_fly()` rend maintenant la main quand les moteurs sont coupés depuis 2 s
  — le désarmement est le seul signe qui ne ment pas, qu'il vienne d'un atterrissage voulu,
  d'un failsafe ou d'un contact avec le sol.
- **La console ne savait décoller qu'une fois par lancement.** `/drone/takeoff` pose maintenant
  un drapeau `req` que le fil consomme à chaque cycle. (Contournement trouvé entre-temps :
  réarmer et redécoller depuis QGC marche, la boucle de commande reprend la main en vol.)
- **`run_gazebo.sh` continuait quand Gazebo n'était jamais monté** : il attendait 30 s le topic
  `/stats`, puis lançait le firmware quand même. Résultat observé : un firmware orphelin qui
  répète `No JSON sensor message received` — un cerveau sans corps, symptôme bruyant dont la
  cause réelle était en **ligne 1** du log Gazebo (`qt.qpa.xcb: could not connect to display`).
  Le script s'arrête maintenant, et **affiche les 5 premières lignes du log** au lieu de laisser
  chercher.
- **Champ de la caméra trop étroit** (1,2 rad, rétréci en juin pour la détection) : la cible
  sort vite du cadre. Arbitraire réel — champ large = cible trop petite pour YOLO. La troisième
  voie serait de faire bouger le gimbal au lieu du drone ; il ne sert quasiment à rien pour
  l'instant.

`gazebo_takeoff_test.py` visait par défaut `tcp:5760`, déjà occupé par mavproxy (le port série
émulé n'accepte qu'un client). Défaut changé pour `udp:14551`, la sortie du fan-out.
