"""console.py — Mode A/B : console opérateur web (détection + lock + guidage closed-loop).

Cœur : source vidéo -> inférence FP16 -> HUD (OpenCV) -> stream MJPEG.
Mode B : clique une détection -> erreur (décalage au centre du VIEWPORT) -> loi
proportionnelle -> yaw-rate streamé au drone SITL. Le viewport (caméra simulée)
PAN avec le cap du drone -> la cible se recentre -> boucle fermée.
Affichage : navigateur (zéro display). http://localhost:8088

Lance : make console   (+ un binaire ArduCopter SITL sur tcp:5760 pour Mode B)
"""
import math
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pymavlink import mavutil
from ultralytics import YOLO

try:
    from gz_camera import GzCamera, GzGimbal, available as gz_available
except Exception:                                     # bindings gz absents -> source gazebo desactivee
    GzCamera, GzGimbal, gz_available = None, None, lambda: False

HERE = Path(__file__).resolve().parent
ENGINE = HERE / "engines" / "best_fp16.engine"        # moteur VisDrone FP16 (videos reelles)
COCO_WEIGHTS = HERE / "yolo11n.pt"                     # COCO (POV Gazebo, domaine synthetique)
IMGSZ = 640
CONF = 0.25
COLORS = {0: (0, 200, 0), 1: (0, 140, 255)}          # BGR : personne=vert, véhicule=orange

# Remap des classes du detecteur -> classes operateur (nom, id unifie 0=personne/1=vehicule).
VISDRONE_MAP = {0: ("personne", 0), 1: ("vehicule", 1)}
COCO_MAP = {0: ("personne", 0), 2: ("vehicule", 1), 3: ("vehicule", 1),
            5: ("vehicule", 1), 7: ("vehicule", 1)}    # person, car, motorcycle, bus, truck

# Loi de guidage en lacet (proportionnelle saturée — port de control_law.hpp).
KP_YAW = 45.0
MAX_YAW_DPS = 40.0

# Caméra simulée (boucle fermée Mode B, sources vidéo)
VP_FRAC = 0.62          # largeur du viewport / largeur pleine
FOV_HALF = 22.0         # demi-plage : ±FOV_HALF° de yaw = pan complet du viewport

# Source Gazebo (POV drone réelle dans la simu 3D).
# Contraintes physiques validées en SITL : l'iris Gazebo NE PEUT PAS yawer ni recevoir de
# yaw_rate, mais il PEUT translater (vitesse NED). Le gimbal est un MOUNT ArduPilot piloté
# en RC override (RC7=pitch, RC8=yaw). => caméra fixe pointée vers l'avant-bas, et on suit
# la cible en TRANSLATANT le drone (strafe pour centrer, avance pour ENGAGE).
GAZEBO = "gazebo"
GZ_CROP_TOP = 0.5       # on retire le haut de l'image (airframe du drone) avant détection
GZ_IMGSZ = 1280         # détection sur image upscalée (cibles synthétiques petites/lointaines)
RC7_PITCH = 1610        # PWM RC7 : pitch caméra avant-bas — À RÉGLER EN LIVE (1500=nadir, +haut=avant)
RC8_YAW = 1500          # PWM RC8 : yaw gimbal neutre (caméra vers l'avant du drone)
K_STRAFE = 2.5          # m/s de strafe (Est/Ouest) par erreur normalisée -> recentre la cible
STRAFE_SIGN = 1.0       # +1 : cible à droite -> strafe Est. Inverser si ça diverge.
ENGAGE_SPEED = 2.0      # m/s avant (Nord caméra) quand l'opérateur ENGAGE le suivi

VIDEOS = {
    "gazebo": ("POV drone · Gazebo (live)", None),
    "vehicles": ("Trafic · top-down", HERE / "assets" / "vehicles.mp4"),
    "people": ("Piétons · top-down", HERE / "assets" / "people.mp4"),
    "fpv": ("Fly-through rue", HERE / "assets" / "fpv.mp4"),
}
DEFAULT = "vehicles"

_state = {"jpeg": None, "dets": [], "fps": 0.0, "dims": None}
_sel = {"name": DEFAULT}
_track = {"locked": False, "cx": 0.0, "cy": 0.0, "error": 0.0, "yaw": 0.0,
          "has": False, "engage": False, "gimbal_yaw": 0.0}
_view = {"pan_x": 0, "vp_w": 0}
_lock = threading.Lock()

# Drone SITL (Mode B). Par défaut udp:14551 = la sortie de mavproxy (run_gazebo.sh) ;
# override possible via ARGOS_DRONE_CONN (ex: tcp:127.0.0.1:5760 pour un SITL nu).
DRONE_CONN = os.environ.get("ARGOS_DRONE_CONN", "udp:127.0.0.1:14551")
TAKEOFF_ALT = 12.0      # cadre les cibles dans la POV gimbal (validé à 12 m)
_YAW_MASK = 0b0000011111000111            # SET_POSITION_TARGET : vitesse + yaw_rate actifs
_VEL_MASK = 0b0000111111000111            # SET_POSITION_TARGET : vitesse seule (ENGAGE)
_drone = {"status": "déconnecté", "armed": False, "alt": 0.0, "hdg": 0.0,
          "flying": False, "href": None}
_drone_started = {"v": False}
_gimbal = {"rc7": RC7_PITCH, "rc8": RC8_YAW}   # réglable en live via /gimbal?pitch=..&yaw=..


def angdiff(a, b):
    return (a - b + 180) % 360 - 180


def yaw_rate_command(error):
    return max(-MAX_YAW_DPS, min(MAX_YAW_DPS, KP_YAW * error))


def detect(result, class_map):
    """Filtre + remappe les boîtes vers les classes opérateur (personne/véhicule)."""
    dets = []
    for b in result.boxes:
        mapped = class_map.get(int(b.cls))
        if mapped is None:
            continue
        name, uid = mapped
        x1, y1, x2, y2 = map(int, b.xyxy[0])
        dets.append({"cls": name, "cls_id": uid, "conf": round(float(b.conf), 2),
                     "box": [x1, y1, x2, y2], "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2})
    return dets


# ─────────────────────────────────────────────────────────────────────────
#  draw_boxes / track_update — LE CANVAS DE VICTOR (look du HUD).
#  Tout se dessine sur le VIEWPORT ; les détections sont décalées de -pan_x.
# ─────────────────────────────────────────────────────────────────────────
def draw_boxes(view, dets_full, pan_x, fps):
    h, w = view.shape[:2]
    n_p = n_v = 0
    for d in dets_full:
        x1, y1, x2, y2 = d["box"]
        vx1, vx2 = x1 - pan_x, x2 - pan_x
        if vx2 < 0 or vx1 > w:
            continue
        color = COLORS.get(d["cls_id"], (200, 200, 200))
        cv2.rectangle(view, (vx1, y1), (vx2, y2), color, 2)
        cv2.putText(view, f"{d['cls']} {d['conf']:.2f}", (vx1, max(12, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        n_p += d["cls"] == "personne"
        n_v += d["cls"] == "vehicule"
    cv2.rectangle(view, (0, 0), (w, 30), (0, 0, 0), -1)
    cv2.putText(view, f"ARGOS Mode A/B   personnes {n_p}   vehicules {n_v}   {fps:.0f} FPS",
                (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)


def track_update(view, dets_full, pan_x, vp_w):
    h, w = view.shape[:2]
    with _lock:
        if not _track["locked"]:
            _track["has"] = False
            return
        cx, cy = _track["cx"], _track["cy"]      # coords PLEINES (stables au pan)

    best, bestd = None, 1e18
    for d in dets_full:
        dd = (d["cx"] - cx) ** 2 + (d["cy"] - cy) ** 2
        if dd < bestd:
            best, bestd = d, dd
    found = best is not None and bestd < (vp_w * 0.16) ** 2
    if found:
        cx, cy = best["cx"], best["cy"]

    vp_center = pan_x + vp_w / 2
    error = (cx - vp_center) / (vp_w / 2)         # -1 gauche .. +1 droite (du viewport)
    yaw = yaw_rate_command(error)

    vx, vy = int(cx - pan_x), int(cy)
    col = (255, 180, 80)
    cv2.line(view, (w // 2, h // 2), (vx, vy), col, 1, cv2.LINE_AA)
    cv2.circle(view, (vx, vy), 20, (0, 0, 255) if found else (130, 130, 130), 2, cv2.LINE_AA)
    cv2.drawMarker(view, (vx, vy), (0, 0, 255), cv2.MARKER_CROSS, 28, 1, cv2.LINE_AA)
    cv2.putText(view, f"LOCK  err {error:+.2f}  yaw {yaw:+.0f} deg/s",
                (10, h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2, cv2.LINE_AA)

    with _lock:
        # cible perdue -> erreur de commande 0 (sinon une erreur figée fait dériver le gimbal)
        _track.update({"cx": cx, "cy": cy, "error": round(error, 3) if found else 0.0,
                       "yaw": round(yaw, 1) if found else 0.0, "has": found})


def _publish(view, dets_full, fps, w, h, pan_x, vp_w):
    ok, buf = cv2.imencode(".jpg", view)
    if ok:
        with _lock:
            _state["jpeg"] = buf.tobytes()
            _state["dets"] = dets_full
            _state["fps"] = fps
            _state["dims"] = (w, h)
            _view.update({"pan_x": pan_x, "vp_w": vp_w})


def _video_loop(model, name):
    """Source vidéo (Mode A) : VisDrone FP16 + viewport-pan piloté par le cap (Mode B simulé)."""
    cap = cv2.VideoCapture(str(VIDEOS[name][1]))
    if not cap.isOpened():
        print(f"[console] source illisible: {VIDEOS[name][1]}")
        time.sleep(2)
        return
    t_prev = time.time()
    while True:
        with _lock:
            if _sel["name"] != name:
                break
        ok, frame = cap.read()
        if not ok:
            break
        H_full, W_full = frame.shape[:2]
        result = model.predict(frame, imgsz=IMGSZ, conf=CONF, device=0, verbose=False)[0]
        dets_full = detect(result, VISDRONE_MAP)

        # viewport = caméra simulée, pan piloté par le cap du drone
        with _lock:
            flying, hdg, href = _drone["flying"], _drone["hdg"], _drone["href"]
        if flying and href is not None:
            vp_w = int(W_full * VP_FRAC)
            pan_max = W_full - vp_w
            dh = angdiff(hdg, href)
            pan_x = int(max(0, min(pan_max, pan_max * 0.5 * (1 + dh / FOV_HALF))))
        else:
            vp_w, pan_x = W_full, 0
        view = frame[:, pan_x:pan_x + vp_w].copy()

        now = time.time()
        fps = 1.0 / max(now - t_prev, 1e-6)
        t_prev = now
        draw_boxes(view, dets_full, pan_x, fps)
        track_update(view, dets_full, pan_x, vp_w)
        _publish(view, dets_full, fps, W_full, H_full, pan_x, vp_w)
    cap.release()


def _gazebo_loop(coco, cam):
    """Source Gazebo : POV RÉELLE du drone (détection + HUD). Le gimbal (RC override) et le
    suivi par TRANSLATION sont gérés dans _drone_thread (qui détient la connexion MAVLink)."""
    t_prev = time.time()
    while True:
        with _lock:
            if _sel["name"] != GAZEBO:
                return
        ok, frame = cam.read()
        if not ok:
            time.sleep(0.03)
            continue
        H, W = frame.shape[:2]
        view = frame[int(GZ_CROP_TOP * H):, :].copy()        # retire le haut (airframe)
        Hc, Wc = view.shape[:2]
        result = coco.predict(view, imgsz=GZ_IMGSZ, conf=CONF, device=0, verbose=False)[0]
        dets_full = detect(result, COCO_MAP)

        pan_x, vp_w = 0, Wc                                   # caméra réelle : aucun pan
        now = time.time()
        fps = 1.0 / max(now - t_prev, 1e-6)
        t_prev = now
        draw_boxes(view, dets_full, pan_x, fps)
        track_update(view, dets_full, pan_x, vp_w)
        _publish(view, dets_full, fps, Wc, Hc, pan_x, vp_w)




def worker():
    visdrone = YOLO(str(ENGINE), task="detect")
    coco = None
    cam = None
    while True:
        with _lock:
            name = _sel["name"]
        if name == GAZEBO:
            if not gz_available():
                print("[console] source gazebo indispo (bindings gz manquants)")
                with _lock:
                    _sel["name"] = DEFAULT
                continue
            try:
                if coco is None:
                    coco = YOLO(str(COCO_WEIGHTS))
                if cam is None:
                    cam = GzCamera()
            except Exception as e:
                print(f"[console] init gazebo échec: {e}")
                with _lock:
                    _sel["name"] = DEFAULT
                time.sleep(1)
                continue
            _gazebo_loop(coco, cam)
        else:
            _video_loop(visdrone, name)


def _drone_thread():
    """Connecte le SITL, décolle en GUIDED, capture le cap de référence, puis
    stream le yaw-rate de la loi tant qu'une cible est verrouillée."""
    m = mavutil.mavlink_connection(DRONE_CONN)
    if not m.wait_heartbeat(timeout=10):
        with _lock:
            _drone["status"] = "pas de SITL (tcp:5760)"
        _drone_started["v"] = False
        return
    m.mav.request_data_stream_send(m.target_system, m.target_component,
                                   mavutil.mavlink.MAV_DATA_STREAM_ALL, 5, 1)
    m.mav.param_set_send(m.target_system, m.target_component, b"ARMING_CHECK", 0,
                         mavutil.mavlink.MAV_PARAM_TYPE_INT32)
    with _lock:
        _drone["status"] = "connecté · attente GPS..."
    # attendre un fix GPS 3D (EKF prêt) — sinon le décollage GUIDED ne monte pas
    t0 = time.time()
    while time.time() - t0 < 40:
        g = m.recv_match(type="GPS_RAW_INT", blocking=True, timeout=1)
        if g and g.fix_type >= 3:
            break
    with _lock:
        _drone["status"] = "connecté · décollage..."

    m.set_mode(m.mode_mapping()["GUIDED"])
    time.sleep(1)
    m.mav.command_long_send(m.target_system, m.target_component,
                            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0)
    t0 = time.time()
    while time.time() - t0 < 15:
        if m.recv_match(type="HEARTBEAT", blocking=True, timeout=2) and m.motors_armed():
            break
    m.mav.command_long_send(m.target_system, m.target_component,
                            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 0, TAKEOFF_ALT)
    t0 = time.time()
    while time.time() - t0 < 30:
        p = m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=2)
        if p and p.relative_alt / 1000.0 >= TAKEOFF_ALT * 0.9:
            break
    with _lock:
        _drone["status"] = "EN VOL · Mode B actif"
        _drone["flying"] = True

    last = 0.0
    while True:
        msg = m.recv_match(type=["ATTITUDE", "GLOBAL_POSITION_INT", "HEARTBEAT"],
                           blocking=True, timeout=0.2)
        if msg:
            t = msg.get_type()
            with _lock:
                if t == "ATTITUDE":
                    _drone["hdg"] = round(math.degrees(msg.yaw) % 360, 1)
                    if _drone["href"] is None and _drone["flying"]:
                        _drone["href"] = _drone["hdg"]      # cap de référence (viewport centré)
                elif t == "GLOBAL_POSITION_INT":
                    _drone["alt"] = round(msg.relative_alt / 1000.0, 1)
                elif t == "HEARTBEAT":
                    _drone["armed"] = bool(m.motors_armed())
        now = time.time()
        if now - last >= 0.2:
            last = now
            with _lock:
                src = _sel["name"]
                active = _track["locked"] and _track["has"]
                yaw = _track["yaw"]
                error = _track["error"]
                engage = _track["engage"]
            if src == GAZEBO:
                # Gimbal = mount ArduPilot en RC override : pitch avant-bas + yaw neutre,
                # tenu en continu (l'override expire en ~3 s, on le renvoie à 5 Hz).
                with _lock:
                    rc7, rc8 = _gimbal["rc7"], _gimbal["rc8"]
                m.mav.rc_channels_override_send(m.target_system, m.target_component,
                    65535, 65535, 65535, 65535, 65535, 65535, rc7, rc8)
                # Suivi par TRANSLATION (le drone Gazebo ne peut pas yawer) : strafe Est/Ouest
                # pour recentrer la cible, + avance quand ENGAGE. Le drone garde son cap Nord.
                if active:
                    vE = max(-3.0, min(3.0, STRAFE_SIGN * K_STRAFE * error))
                    vN = ENGAGE_SPEED if engage else 0.0
                    m.mav.set_position_target_local_ned_send(
                        0, m.target_system, m.target_component,
                        mavutil.mavlink.MAV_FRAME_LOCAL_NED, _VEL_MASK,
                        0, 0, 0, vN, vE, 0, 0, 0, 0, 0, 0)
            elif active:
                # sources vidéo (bare SITL) : yaw-rate du drone -> le viewport pan
                m.mav.set_position_target_local_ned_send(
                    0, m.target_system, m.target_component,
                    mavutil.mavlink.MAV_FRAME_LOCAL_NED, _YAW_MASK,
                    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, math.radians(yaw))


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=worker, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


def _mjpeg():
    while True:
        with _lock:
            jpg = _state["jpeg"]
        if jpg is None:
            time.sleep(0.05)
            continue
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
        time.sleep(1 / 15)


@app.get("/stream")
def stream():
    return StreamingResponse(_mjpeg(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/detections")
def detections():
    with _lock:
        return {"fps": round(_state["fps"], 1), "detections": _state["dets"]}


@app.get("/sources")
def sources():
    with _lock:
        cur = _sel["name"]
    return {"current": cur, "videos": [{"name": k, "label": v[0]} for k, v in VIDEOS.items()]}


@app.get("/source")
def set_source(name: str):
    if name in VIDEOS:
        with _lock:
            _sel["name"] = name
    with _lock:
        return {"current": _sel["name"]}


@app.get("/lock")
def lock(fx: float, fy: float):
    with _lock:
        dets, dims = list(_state["dets"]), _state["dims"]
        pan_x, vp_w = _view["pan_x"], _view["vp_w"]
    if not dims or not dets or not vp_w:
        return {"locked": False}
    _, H_full = dims
    full_x = pan_x + fx * vp_w          # clic = fraction du VIEWPORT affiché
    full_y = fy * H_full
    best, bestd = None, 1e18
    for d in dets:
        dd = (d["cx"] - full_x) ** 2 + (d["cy"] - full_y) ** 2
        if dd < bestd:
            best, bestd = d, dd
    if best:
        with _lock:
            _track.update({"locked": True, "cx": best["cx"], "cy": best["cy"]})
    return {"locked": True}


@app.get("/unlock")
def unlock():
    with _lock:
        _track["locked"] = False
        _track["engage"] = False
    return {"locked": False}


@app.get("/engage")
def engage():
    """Active le SUIVI : le drone avance vers la cible (en plus du yaw-centrage)."""
    with _lock:
        if _track["locked"]:
            _track["engage"] = True
        return {"engage": _track["engage"]}


@app.get("/disengage")
def disengage():
    with _lock:
        _track["engage"] = False
    return {"engage": False}


@app.get("/track")
def track():
    with _lock:
        return {k: _track[k] for k in ("locked", "error", "yaw", "has", "engage", "gimbal_yaw")}


@app.get("/drone/takeoff")
def drone_takeoff():
    if not _drone_started["v"]:
        _drone_started["v"] = True
        threading.Thread(target=_drone_thread, daemon=True).start()
    with _lock:
        return dict(_drone)


@app.get("/gimbal")
def gimbal(pitch: int = None, yaw: int = None):
    """Réglage live du gimbal (PWM RC7 pitch / RC8 yaw). Ex: /gimbal?pitch=1650"""
    with _lock:
        if pitch is not None:
            _gimbal["rc7"] = max(1100, min(1900, pitch))
        if yaw is not None:
            _gimbal["rc8"] = max(1100, min(1900, yaw))
        return dict(_gimbal)


@app.get("/drone/status")
def drone_status():
    with _lock:
        return dict(_drone)


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>ARGOS — Mode A/B</title>
<style>
  body{margin:0;background:#0b0f14;color:#cdd6e0;font-family:system-ui,sans-serif;display:flex}
  #video{flex:1;display:flex;align-items:center;justify-content:center;background:#000}
  #video img{max-width:100%;max-height:100vh;cursor:crosshair}
  #panel{width:268px;padding:16px;background:#11161d;border-left:1px solid #1f2733;overflow:auto;max-height:100vh}
  h1{font-size:14px;letter-spacing:.12em;color:#5ec8ff;margin:0 0 14px}
  .lbl{font-size:11px;letter-spacing:.08em;color:#5b6b7c;margin:14px 0 6px}
  .stat{font-size:13px;margin:7px 0;color:#9fb0c0;display:flex;justify-content:space-between}
  .stat b{color:#fff;font-size:17px}
  button.src{display:block;width:100%;text-align:left;margin:5px 0;padding:9px 11px;border-radius:7px;
    border:1px solid #233040;background:#161d26;color:#bcccdb;font-size:13px;cursor:pointer}
  button.src.on{border-color:#5ec8ff;background:#10212e;color:#fff}
  #bar{height:8px;background:#1a2430;border-radius:4px;margin:8px 0;position:relative}
  #barfill{position:absolute;top:-2px;bottom:-2px;width:3px;background:#ff5a4d;border-radius:2px}
  ul{list-style:none;padding:0;margin:8px 0;font-size:12px;max-height:20vh;overflow:auto}
  li{padding:3px 0;color:#7f93a6;border-bottom:1px solid #161d26}
</style></head><body>
  <div id="video"><img id="cam" src="/stream"></div>
  <div id="panel">
    <h1>ARGOS · MODE A/B</h1>
    <div class="lbl">SOURCE</div>
    <div id="menu"></div>
    <div class="lbl">CIBLE — clique une boîte</div>
    <div class="stat"><span>Lock</span><b id="lk">—</b></div>
    <div class="stat"><span>Suivi</span><b id="eng">—</b></div>
    <div class="stat"><span>Erreur</span><b id="er">0</b></div>
    <div class="stat"><span>Yaw cmd</span><b id="yw">0°/s</b></div>
    <div id="bar"><div id="barfill" style="left:50%"></div></div>
    <button class="src" onclick="engage()">ENGAGER le suivi</button>
    <button class="src" onclick="disengage()">Désengager</button>
    <button class="src" onclick="unlock()">Unlock</button>
    <div class="lbl">DRONE — Mode B</div>
    <button class="src" onclick="takeoff()">Décoller + activer Mode B</button>
    <div class="stat"><span>État</span><b id="dst" style="font-size:11px">déconnecté</b></div>
    <div class="stat"><span>Cap</span><b id="dhdg">–</b></div>
    <div class="stat"><span>Alt</span><b id="dalt">–</b></div>
    <div class="lbl">TÉLÉMÉTRIE</div>
    <div class="stat"><span>FPS</span><b id="fps">–</b></div>
    <div class="stat"><span>Personnes</span><b id="np">0</b></div>
    <div class="stat"><span>Véhicules</span><b id="nv">0</b></div>
    <ul id="list"></ul>
  </div>
<script>
const cam=document.getElementById('cam');
cam.addEventListener('click',async e=>{
  const r=cam.getBoundingClientRect();
  const fx=(e.clientX-r.left)/r.width, fy=(e.clientY-r.top)/r.height;
  await fetch(`/lock?fx=${fx.toFixed(4)}&fy=${fy.toFixed(4)}`);
});
async function unlock(){ await fetch('/unlock'); }
async function engage(){ await fetch('/engage'); }
async function disengage(){ await fetch('/disengage'); }
async function takeoff(){ await fetch('/drone/takeoff'); }
async function loadMenu(){
  const s=await (await fetch('/sources')).json();
  menu.innerHTML=s.videos.map(v=>
    `<button class="src${v.name===s.current?' on':''}" onclick="pick('${v.name}')">${v.label}</button>`).join('');
}
async function pick(n){ await fetch('/source?name='+n); loadMenu(); }
async function poll(){
  try{
    const d=await (await fetch('/detections')).json();
    fps.textContent=d.fps;
    const dets=d.detections||[];
    np.textContent=dets.filter(x=>x.cls==='personne').length;
    nv.textContent=dets.filter(x=>x.cls==='vehicule').length;
    list.innerHTML=dets.slice(0,20).map(x=>`<li>${x.cls} · ${x.conf}</li>`).join('');
    const t=await (await fetch('/track')).json();
    lk.textContent=t.locked?(t.has?'ACTIF':'perdu'):'—';
    eng.textContent=t.engage?'ENGAGÉ':'—';
    eng.style.color=t.engage?'#ff5a4d':'#fff';
    er.textContent=(t.error??0).toFixed(2);
    yw.textContent=(t.yaw??0).toFixed(0)+'°/s';
    barfill.style.left=Math.max(0,Math.min(100,50+(t.yaw||0)/45*50))+'%';
    const ds=await (await fetch('/drone/status')).json();
    dst.textContent=ds.status; dhdg.textContent=(ds.hdg??0)+'°'; dalt.textContent=(ds.alt??0)+' m';
  }catch(e){}
  setTimeout(poll,300);
}
loadMenu(); poll();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ARGOS_PORT", "8088")))
