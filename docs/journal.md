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
