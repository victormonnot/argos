"""console.py — Mode A/B : console opérateur web (détection + lock + guidage closed-loop).

Cœur : source vidéo -> inférence FP16 -> HUD (OpenCV) -> stream MJPEG.
Mode B : clique une détection -> erreur pixel -> loi d'attitude -> SET_ATTITUDE_TARGET
en **GUIDED_NOGPS (mode 20)**. Zéro estimation de position : pas de GPS, pas de flow.
L'altitude est tenue par ArduPilot au baro (`thrust = 0,5`), la distance est mesurée
par la TAILLE de la bounding box, et le cap n'est commandé qu'en RELATIF.
Affichage : navigateur (zéro display). http://localhost:8088

Ce fichier ne décide plus et n'encode plus : il orchestre. La loi vit dans
`control/guidance.py`, les garde-fous dans `control/gate.py`, et le seul endroit
qui parle MAVLink pour piloter est `control/mavlink_backend.py` (PORTFOLIO §1.5-A/B).

Lance : make console   (+ un binaire ArduCopter SITL sur tcp:5760 pour Mode B)
"""
import math
import os
import random
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
from fastapi import FastAPI
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               StreamingResponse)
from pymavlink import mavutil
from ultralytics import YOLO

from control import (CommandGate, GuidanceGains, Limits, TargetView, VehicleState,
                     VisualGuidance)
from control.guidance import DEG, operator_command
from control.link import LinkStats
from control.mavlink_backend import MavlinkBackend

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
RC6_ROLL = 1500         # PWM RC6 : roll gimbal neutre (caméra à plat, pas bancale)
RC7_PITCH = 1610        # PWM RC7 : pitch caméra avant-bas — À RÉGLER EN LIVE (1500=nadir, +haut=avant)
RC8_YAW = 1500          # PWM RC8 : yaw gimbal neutre (caméra vers l'avant du drone)

# ── Profils de gains : une seule loi, deux capteurs très différents ──────────
# gazebo : caméra réelle sur gimbal FIXE. L'iris Gazebo n'a pas de couple de lacet
#   (vérifié 2026-06-19) -> le recentrage se fait au ROLL. Le dyaw reste petit mais
#   non nul : c'est le chemin qui comptera sur le vrai drone, et comme il est
#   recalculé à partir du cap MESURÉ à chaque cycle, un lacet qui ne suit pas ne
#   fait jamais diverger le repère de l'attitude commandée.
# video : hack viewport (SITL nu, pas de caméra). Là le drone YAW pour de vrai et
#   c'est le lacet qui fait paner la fenêtre -> gain de cap fort, roll inutile.
# Les valeurs du profil gazebo ont été réglées EN VOL via /tune, pas calculées :
# kp descendu (7 -> 4) et kd monté (9 -> 12) parce que le centrage était nerveux,
# kd_size ajouté parce que sans lui l'oscillation d'approche divergeait.
GAINS = {
    GAZEBO:  GuidanceGains(kp_roll=4.0 * DEG, kd_roll=12.0 * DEG, kp_yaw=1.5 * DEG,
                           k_pitch=3.5 * DEG, kd_size=10.0 * DEG),
    "video": GuidanceGains(kp_roll=0.0, kd_roll=0.0, kp_yaw=5.0 * DEG, k_pitch=0.0),
}
LIMITS = Limits(size_stop=GAINS[GAZEBO].size_near)   # bornes dures de la porte de
                        # sortie : indépendantes des gains, SAUF la garde de proximité
                        # qu'on aligne sur la loi pour qu'elles ne divergent pas.
CMD_HZ = 10.0           # cadence du flux SET_ATTITUDE_TARGET
GUID_TIMEOUT = 1.0      # s — au-delà, ArduPilot remet à plat (mode_guided.cpp:983).
                        # Était à 0,5 s (valeur recommandée au §1.1). Remonté après
                        # MESURE : la boucle de commande, qui partage le GPU et le GIL
                        # avec YOLO, bégaie jusqu'à 0,8 s — donc elle déclenchait le
                        # filet toute seule, en plein vol normal, sans aucun message.
                        # 1,0 s couvre le bégaiement observé. Le vrai correctif est de
                        # fiabiliser la boucle ; celui-ci n'est qu'un pansement mesuré.

VIDEOS = {
    "gazebo": ("POV drone · Gazebo (live)", None),
    "vehicles": ("Trafic · top-down", HERE / "assets" / "vehicles.mp4"),
    "people": ("Piétons · top-down", HERE / "assets" / "people.mp4"),
    "fpv": ("Fly-through rue", HERE / "assets" / "fpv.mp4"),
}
DEFAULT = "vehicles"

_state = {"jpeg": None, "dets": [], "fps": 0.0, "dims": None}
_sel = {"name": DEFAULT}
_track = {"locked": False, "cx": 0.0, "cy": 0.0, "error": 0.0, "size": 0.0,
          "has": False, "found": False, "engage": False, "gimbal_yaw": 0.0,
          "cls_id": None, "last_found": 0.0, "seq": 0}
# réglages de suivi ajustables en live via /tune (sans redémarrer la console)
_tune = {"gate": 0.35, "coast": 1.5}   # coast = secondes de maintien après perte
_view = {"pan_x": 0, "vp_w": 0}
_lock = threading.Lock()

# Drone SITL (Mode B). Par défaut udp:14551 = la sortie de mavproxy (run_gazebo.sh) ;
# override possible via ARGOS_DRONE_CONN (ex: tcp:127.0.0.1:5760 pour un SITL nu).
DRONE_CONN = os.environ.get("ARGOS_DRONE_CONN", "udp:127.0.0.1:14551")
TAKEOFF_ALT = 12.0      # cadre les cibles dans la POV gimbal (validé à 12 m)
_drone = {"status": "déconnecté", "armed": False, "alt": 0.0, "hdg": 0.0,
          "roll": 0.0, "pitch": 0.0,
          "flying": False, "href": None, "mode": "-", "req": False}
                        # req : l'opérateur a appuyé sur « Décoller ». Le fil de vol
                        # le consomme et repart pour un cycle complet — c'est ce qui
                        # permet de redécoller sans redémarrer la console.
_drone_started = {"v": False}
_gimbal = {"rc7": RC7_PITCH, "rc8": RC8_YAW}   # réglable en live via /gimbal?pitch=..&yaw=..
# Vol manuel : une INTENTION normalisée -1..1, pas une vitesse. En GUIDED_NOGPS il
# n'existe pas de consigne de vitesse — « avancer » est un angle de piqué. Et cette
# intention repart par la MÊME porte de sortie que le suivi (§1.5-A).
_manual = {"fwd": 0.0, "right": 0.0, "up": 0.0, "until": 0.0}
# Dernière commande réellement émise — c'est ce que le HUD affiche (pas l'intention).
_cmd = {"src": "idle", "roll": 0.0, "pitch": 0.0, "dyaw": 0.0, "thrust": 0.5,
        "reasons": [], "sent": 0, "approach": 0.0, "size": 0.0}

# Instrumentation de la liaison (PORTFOLIO §1.5-C). Une instance = une liaison ;
# le jour ou il y en a deux (§1.2 : vraie radio + WiFi), on en met deux.
_link = LinkStats(fenetre=3.0)
PING_HZ = 2.0           # cadence des requetes TIMESYNC (mesure de latence)
# Degradation VOLONTAIRE de la reception, reglable en vol via /degrade?perte=0.1.
# Sur TCP en local la perte est nulle par construction : sans ce robinet, le
# compteur de pertes ne serait jamais ni etalonne ni exerce. Et ca donne le banc
# de degradation que reclame la phase 2 du swarm, sans materiel.
_degrade = {"perte": 0.0}

# Sonde de coupure (PORTFOLIO §1.5-D). On cesse volontairement d'émettre pendant
# `cut_ms`, et on enregistre ce que le drone fait — pendant la coupure et après.
# `GUID_TIMEOUT` (0,5 s) dit ce qu'ArduPilot est CENSÉ faire : remettre l'attitude
# à plat au cap courant, annuler les rates, et forcer `use_thrust = false`
# (mode_guided.cpp:983). Cette sonde vérifie ce qu'il fait VRAIMENT, et en combien
# de temps. C'est la phase 3 du plan swarm, acquise sur un seul drone.
CUT_TAIL = 3.0          # s d'enregistrement APRÈS la reprise (voir la récupération)
_cut = {"t0": 0.0, "silence": 0.0, "until": 0.0, "end": 0.0, "ms": 0,
        "trace": [], "running": False}


def angdiff(a, b):
    return (a - b + 180) % 360 - 180


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
    now = time.time()
    with _lock:
        if not _track["locked"]:
            _track.update({"has": False, "found": False})
            return
        cx, cy = _track["cx"], _track["cy"]      # coords PLEINES (stables au pan)
        tcls = _track.get("cls_id")
        gate = _tune["gate"]
        coast_t = _tune["coast"]
        last_found = _track["last_found"]
        last_err = _track["error"]
        size = _track["size"]

    # ne suivre que la classe verrouillée (1 seule personne dans la scène -> robuste au
    # flicker : on raccroche la cible où qu'elle soit, avec une gate généreuse).
    cand = [d for d in dets_full if tcls is None or d["cls_id"] == tcls]
    best, bestd = None, 1e18
    for d in cand:
        dd = (d["cx"] - cx) ** 2 + (d["cy"] - cy) ** 2
        if dd < bestd:
            best, bestd = d, dd
    found = best is not None and bestd < (vp_w * gate) ** 2
    if found:
        cx, cy = best["cx"], best["cy"]
        last_found = now
        # LE capteur de distance (§1.1) : hauteur de la boîte / hauteur image.
        # Ça grandit = on approche. Aucun état de position n'est impliqué.
        size = (best["box"][3] - best["box"][1]) / max(h, 1)

    # COAST : la cible reste "active" un court instant après une perte (flicker détection)
    # -> le drone continue de strafer vers sa dernière position au lieu de tout lâcher.
    coasting = (now - last_found) < coast_t
    has = found or coasting
    vp_center = pan_x + vp_w / 2
    error = (cx - vp_center) / (vp_w / 2) if found else last_err

    vx, vy = int(cx - pan_x), int(cy)
    col = (255, 180, 80) if found else (120, 120, 120)
    cv2.line(view, (w // 2, h // 2), (vx, vy), col, 1, cv2.LINE_AA)
    cv2.circle(view, (vx, vy), 20, (0, 0, 255) if found else (130, 130, 130), 2, cv2.LINE_AA)
    cv2.drawMarker(view, (vx, vy), (0, 0, 255), cv2.MARKER_CROSS, 28, 1, cv2.LINE_AA)
    cv2.putText(view, f"LOCK  err {error:+.2f}  taille {size:.2f}  "
                      f"{'TRACK' if found else 'coast'}",
                (10, h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2, cv2.LINE_AA)

    with _lock:
        _track.update({"cx": cx, "cy": cy, "last_found": last_found,
                       "error": round(error, 3) if has else 0.0,
                       "size": round(size, 3), "has": has, "found": found})


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


def _absorb(m, msg, backend=None):
    """Compte le message pour la liaison, puis range ce qu'on sait ranger.

    L'ordre importe : on COMPTE d'abord, tous types confondus. Mesurer une
    liaison en n'en lisant qu'une partie fabriquerait des trous de sequence
    indiscernables de vraies pertes (§1.5-C).
    """
    if not msg:
        return
    if _degrade["perte"] and random.random() < _degrade["perte"]:
        return              # jete AVANT tout comptage : le trou apparait donc dans
                            # la suite des sequences, exactement comme une vraie
                            # perte radio. Et la boucle de vol en souffre pour de bon.
    t = msg.get_type()
    if t != "BAD_DATA":
        _link.on_rx(time.time(), msg.get_srcSystem(), msg.get_srcComponent(),
                    msg.get_seq(), len(msg.get_msgbuf()), t)
        if t == "TIMESYNC" and backend is not None:
            rtt = backend.pong(msg)
            if rtt is not None:
                _link.on_rtt(time.time(), rtt)
    with _lock:
        if t == "ATTITUDE":
            _drone["hdg"] = round(math.degrees(msg.yaw) % 360, 1)
            # Assiette RÉELLE, distincte de l'assiette commandée : c'est l'écart
            # entre les deux que mesure la sonde de coupure.
            _drone["roll"] = round(math.degrees(msg.roll), 1)
            _drone["pitch"] = round(math.degrees(msg.pitch), 1)
            if _drone["href"] is None and _drone["flying"]:
                _drone["href"] = _drone["hdg"]      # cap de reference (viewport centre)
        elif t == "GLOBAL_POSITION_INT":
            _drone["alt"] = round(msg.relative_alt / 1000.0, 1)
        elif t == "HEARTBEAT":
            _drone["armed"] = bool(m.motors_armed())
            _drone["mode"] = m.flightmode


def _cut_sample(now, cmd):
    """Un point de la trace de coupure. `cmd` vaut None quand on est en train de
    se taire — c'est ce qui distingue les deux phases dans l'enregistrement."""
    with _lock:
        if not _cut["running"]:
            return
        if now > _cut["end"]:
            _cut["running"] = False
            return
        _cut["trace"].append({
            "t": round(now - _cut["t0"], 2),
            "emis": cmd is not None,
            "roll_cmd": None if cmd is None else round(math.degrees(cmd.roll), 1),
            "pitch_cmd": None if cmd is None else round(math.degrees(cmd.pitch), 1),
            "roll": _drone["roll"], "pitch": _drone["pitch"],
            "alt": _drone["alt"], "mode": _drone["mode"],
        })


def _gimbal_hold(m, tick):
    """Tient la consigne du gimbal a 5 Hz, EN PERMANENCE.

    Un override RC expire cote ArduPilot au bout de `RC_OVERRIDE_TIME` (3 s) : il
    faut le reemettre en continu. Et il faut le faire **avant et pendant** le
    decollage, sinon la camera pend librement jusqu'a ce que la boucle de vol
    demarre — c'est l'image de travers observee au sol puis redressee en l'air.

    C'est une commande de CHARGE UTILE : elle ne peut pas deplacer le drone, elle
    ne passe donc pas par la porte de sortie.
    """
    now = time.time()
    if now - tick["t"] < 0.2:
        return
    tick["t"] = now
    with _lock:
        if _sel["name"] != GAZEBO:
            return
        rc7, rc8 = _gimbal["rc7"], _gimbal["rc8"]
    m.mav.rc_channels_override_send(m.target_system, m.target_component,
        65535, 65535, 65535, 65535, 65535, RC6_ROLL, rc7, rc8)


def _takeoff(m, tick, backend):
    """Decollage en GUIDED (donc au GPS) : monter est un probleme de position, et
    ce n'est pas celui qu'on demontre. GUIDED_NOGPS vient juste apres.

    Rend True seulement si le drone est REELLEMENT en l'air. Un decollage rate
    qu'on declare reussi, c'est un flux d'attitude envoye a un drone au sol —
    et en mode 20 `thrust = 0,5` sur un drone pose, c'est un ordre de decollage
    en attente (`angle_control_run()` teste `land_complete`).
    """
    m.mav.request_data_stream_send(m.target_system, m.target_component,
                                   mavutil.mavlink.MAV_DATA_STREAM_ALL, 5, 1)
    m.mav.param_set_send(m.target_system, m.target_component, b"ARMING_CHECK", 0,
                         mavutil.mavlink.MAV_PARAM_TYPE_INT32)
    # WP_YAW_BEHAVIOR=0 : le drone NE tourne PAS le nez vers sa vitesse (sinon chaque
    # correction ferait pivoter la camera hors cible). Indispensable en gimbal fixe.
    m.mav.param_set_send(m.target_system, m.target_component, b"WP_YAW_BEHAVIOR", 0,
                         mavutil.mavlink.MAV_PARAM_TYPE_INT32)
    time.sleep(0.3)
    with _lock:
        _drone["status"] = "connecté · attente GPS..."
    # attendre un fix GPS 3D (EKF pret) — sinon le decollage GUIDED ne monte pas
    t0 = time.time()
    while time.time() - t0 < 40:
        msg = m.recv_match(blocking=True, timeout=1)
        _absorb(m, msg, backend)
        _gimbal_hold(m, tick)
        if msg and msg.get_type() == "GPS_RAW_INT" and msg.fix_type >= 3:
            break
    with _lock:
        _drone["status"] = "connecté · décollage..."

    m.set_mode(m.mode_mapping()["GUIDED"])
    time.sleep(1)
    # L'EKF peut encore converger ("Need Position Estimate") : on redemande au lieu
    # d'abandonner sur un seul refus. ARMING_CHECK=0 ne couvre pas cette exigence-la,
    # elle vient du mode GUIDED lui-meme.
    t0 = time.time()
    while time.time() - t0 < 60 and not m.motors_armed():
        m.mav.command_long_send(m.target_system, m.target_component,
                                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
                                1, 0, 0, 0, 0, 0, 0)
        t1 = time.time()
        while time.time() - t1 < 3:
            _absorb(m, m.recv_match(blocking=True, timeout=1), backend)
            _gimbal_hold(m, tick)
    if not m.motors_armed():
        return False

    m.mav.command_long_send(m.target_system, m.target_component,
                            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 0, TAKEOFF_ALT)
    t0 = time.time()
    while time.time() - t0 < 30:
        msg = m.recv_match(blocking=True, timeout=2)
        _absorb(m, msg, backend)
        _gimbal_hold(m, tick)
        if (msg and msg.get_type() == "GLOBAL_POSITION_INT"
                and msg.relative_alt / 1000.0 >= TAKEOFF_ALT * 0.9):
            return True
    return False


def _wait_request(m, tick, backend):
    """Au sol : on draine la liaison et on tient le gimbal, en attendant que
    l'operateur appuie sur « Decoller ». Aucune commande de vol n'est emise."""
    while True:
        with _lock:
            if _drone["req"]:
                _drone["req"] = False
                return
        _absorb(m, m.recv_match(blocking=True, timeout=0.05), backend)
        _gimbal_hold(m, tick)


def _fly(m, backend, gate, tick):
    """La boucle de vol, a CMD_HZ. Rend la main quand le drone n'est plus en l'air.

        erreur pixel + taille de bbox  ->  loi (guidance.py)  ->  AttitudeCmd
                                       ->  porte de sortie (gate.py)
                                       ->  backend MAVLink    ->  SET_ATTITUDE_TARGET

    Aucune estimation de position n'entre dans la commande : ni GPS, ni flow, ni
    vitesse. L'altitude est fermee par ArduPilot au baro (`thrust = 0,5`).
    """
    with _lock:
        src = _sel["name"]
    guidance = VisualGuidance(GAINS[GAZEBO if src == GAZEBO else "video"])
    t_cmd = time.time()
    t_ping = 0.0
    seq_seen = -1
    t_disarm = 0.0
    octets_tx = backend.bytes_sent
    while True:
        _absorb(m, m.recv_match(blocking=True, timeout=0.02), backend)
        now = time.time()
        _gimbal_hold(m, tick)
        if now - t_ping >= 1.0 / PING_HZ:      # mesure de latence (§1.5-C)
            t_ping = now
            backend.ping()

        # Fin de vol. Les moteurs coupes sont le seul signe qui ne ment pas :
        # atterrissage volontaire, failsafe, ou contact avec le sol. Sans ca, la
        # console continuait d'afficher « EN VOL » sur un drone pose et desarme.
        with _lock:
            armed = _drone["armed"]
        if armed:
            t_disarm = 0.0
        elif t_disarm == 0.0:
            t_disarm = now
        elif now - t_disarm > 2.0:
            return

        if now - t_cmd < 1.0 / CMD_HZ:
            continue
        dt, t_cmd = now - t_cmd, now

        with _lock:
            src = _sel["name"]
            state = VehicleState(flying=_drone["flying"],
                                 heading=math.radians(_drone["hdg"]),
                                 alt=_drone["alt"])
            target = TargetView(has=_track["locked"] and _track["has"],
                                found=_track["found"], error_x=_track["error"],
                                size=_track["size"])
            engage, seq = _track["engage"], _track["seq"]
            man = dict(_manual)

        # profil de gains suivi de la source ; nouveau lock -> memoire du D remise a zero
        want = GAZEBO if src == GAZEBO else "video"
        if GAINS[want] is not guidance.g:
            guidance.g = GAINS[want]
        if seq != seq_seen:
            seq_seen = seq
            guidance.reset()

        # ── QUI commande ce cycle ─────────────────────────────────────────────
        # Un seul emetteur a la fois, et tous sortent par la meme porte.
        if now < man["until"]:
            cmd = operator_command(man["fwd"], man["right"], man["up"],
                                   max_tilt=LIMITS.max_tilt)
        elif target.has:
            cmd = guidance.step(target, engage, dt)
        else:
            cmd = guidance.step(TargetView(has=False), False, dt)   # remet la loi a zero

        # ── Sonde de coupure : on n'emet RIEN, volontairement ─────────────────
        # Une coupure ne se simule pas en envoyant des zeros : envoyer une
        # attitude nulle, c'est encore parler. Il faut vraiment se taire, et
        # laisser `GUID_TIMEOUT` expirer cote firmware.
        with _lock:
            coupe = _cut["silence"] <= now < _cut["until"]
            trace_on = _cut["running"]
        if coupe:
            _cut_sample(now, None)
            continue

        res = gate.submit(cmd, state, target)
        # Comptage TX : tout ce qui est parti depuis le cycle precedent (commande,
        # gimbal, ping). Le "trou max" qui en sort est ce qui a revele le begaiement
        # de la boucle — il se mesure maintenant en continu, pas seulement en sonde.
        _link.on_tx(now, backend.bytes_sent - octets_tx)
        octets_tx = backend.bytes_sent
        if trace_on:
            _cut_sample(now, res.cmd)
        with _lock:
            _cmd.update({"src": res.cmd.source,
                         "roll": round(math.degrees(res.cmd.roll), 1),
                         "pitch": round(math.degrees(res.cmd.pitch), 1),
                         "dyaw": round(math.degrees(res.cmd.dyaw), 1),
                         "thrust": round(res.cmd.thrust, 2),
                         "reasons": res.reasons, "sent": backend.sent,
                         "approach": guidance.telemetry["approach"],
                         "size": round(target.size, 2)})


def _drone_thread():
    """Orchestre, et rien d'autre : ne decide pas, n'encode pas.

    Cycle de vie complet, en boucle : attendre l'ordre -> decoller en GUIDED ->
    basculer en GUIDED_NOGPS -> voler -> detecter le retour au sol -> recommencer.
    Le fil vit tant que la console vit, donc on peut redecoller autant de fois
    qu'on veut sans redemarrer.
    """
    m = mavutil.mavlink_connection(DRONE_CONN)
    if not m.wait_heartbeat(timeout=10):
        with _lock:
            _drone["status"] = f"pas de drone sur {DRONE_CONN}"
        _drone_started["v"] = False
        return

    backend = MavlinkBackend(m)
    gate = CommandGate(backend, LIMITS)
    backend.configure_nogps(GUID_TIMEOUT)
    tick = {"t": 0.0}                     # cadence propre au gimbal

    while True:
        _wait_request(m, tick, backend)
        if not _takeoff(m, tick, backend):
            with _lock:
                _drone["status"] = "décollage ÉCHOUÉ · réappuyer pour réessayer"
            continue                      # `flying` reste False : la porte refuse tout

        # La bascule. `ModeGuidedNoGPS::requires_position()` est false -> pas de
        # blocage EKF ; le handler SET_ATTITUDE_TARGET n'accepte que si `in_guided_mode()`.
        ok_mode = backend.set_mode("GUIDED_NOGPS")
        time.sleep(0.5)
        with _lock:
            _drone["status"] = ("EN VOL · GUIDED_NOGPS" if ok_mode
                                else "EN VOL · mode 20 INDISPONIBLE")
            _drone["flying"] = True

        _fly(m, backend, gate, tick)

        with _lock:
            _drone.update({"flying": False, "href": None,
                           "status": "posé · prêt à redécoller"})


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
            # `seq` s'incrémente à chaque nouveau lock : le fil drone y voit le
            # signal de repartir de zéro (le terme dérivé de la loi n'a rien à
            # apprendre de la cible précédente).
            _track.update({"locked": True, "cx": best["cx"], "cy": best["cy"],
                           "cls_id": best["cls_id"], "size": 0.0,
                           "seq": _track.get("seq", 0) + 1})
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
        return {k: _track[k] for k in ("locked", "error", "size", "has", "found", "engage")}


@app.get("/command")
def command():
    """La dernière commande RÉELLEMENT émise, telle que la porte de sortie l'a
    laissée passer (angles en degrés) — pas l'intention en amont."""
    with _lock:
        return dict(_cmd)


@app.get("/drone/takeoff")
def drone_takeoff():
    """Demande un décollage. Le premier appel démarre le fil de vol ; les suivants
    ne font que reposer le drapeau — le fil, lui, ne meurt jamais."""
    with _lock:
        _drone["req"] = True
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


@app.get("/tune")
def tune(gate: float = None, coast: float = None, kp: float = None, kd: float = None,
         kpitch: float = None, kdsize: float = None, near: float = None):
    """Réglage live, sans redémarrer la console.

    Suivi : `gate` (rayon de raccroche), `coast` (s de maintien après perte).
    Loi (degrés, sur le profil de la source courante) :
      `kp`/`kd`  = inclinaison latérale, P et D
      `kpitch`   = piqué d'approche
      `kdsize`   = amortissement de l'approche (D sur la vitesse de grossissement
                   de la bbox). À 0, l'axe avant/arrière n'a aucun frein et
                   l'oscillation d'approche diverge.
      `near`     = taille de bbox à laquelle on arrête d'avancer."""
    with _lock:
        g = GAINS[GAZEBO if _sel["name"] == GAZEBO else "video"]
        if gate is not None:
            _tune["gate"] = max(0.05, min(0.9, gate))
        if coast is not None:
            _tune["coast"] = max(0.0, min(5.0, coast))
        if kp is not None:
            g.kp_roll = max(0.0, min(20.0, kp)) * DEG
        if kd is not None:
            g.kd_roll = max(0.0, min(30.0, kd)) * DEG
        if kpitch is not None:
            g.k_pitch = max(0.0, min(15.0, kpitch)) * DEG
        if kdsize is not None:
            g.kd_size = max(0.0, min(40.0, kdsize)) * DEG
        if near is not None:
            g.size_near = max(0.05, min(0.9, near))
            LIMITS.size_stop = g.size_near      # la garde dure suit le réglage
        return {**_tune, "kp": round(g.kp_roll / DEG, 1), "kd": round(g.kd_roll / DEG, 1),
                "kpitch": round(g.k_pitch / DEG, 1),
                "kdsize": round(g.kd_size / DEG, 1), "near": round(g.size_near, 2)}


@app.get("/fly")
def fly(fwd: float = 0.0, right: float = 0.0, up: float = 0.0, dur: float = 1.2):
    """Vol manuel opérateur : une INTENTION -1..1 pendant `dur` s (override le suivi).

    En GUIDED_NOGPS il n'y a pas de consigne de vitesse — « avancer » est un angle
    de piqué. Et cette commande passe par la même porte de sortie que le suivi :
    l'opérateur ne peut pas non plus forcer l'approche d'une cible trop proche."""
    with _lock:
        _manual.update({"fwd": fwd, "right": right, "up": up,
                        "until": time.time() + max(0.2, min(3.0, dur))})
    return {"manual": True, "fwd": fwd, "right": right, "up": up}


@app.get("/link", response_class=PlainTextResponse)
def link(json: int = 0):
    """État de la liaison (PORTFOLIO §1.5-C). Texte par défaut, `?json=1` sinon.

    La perte de paquets ne coûte rien à mesurer : les messages MAVLink sont
    numérotés, les trous dans la suite la donnent. Aucun champ ajouté, aucun
    message de test, aucun accord avec l'autre bout."""
    s = _link.snapshot(time.time())
    if json:
        return JSONResponse(s.__dict__)

    def ms(v):
        return "—" if v is None else f"{v:.1f} ms"

    lignes = [
        f"LIAISON {DRONE_CONN}   (fenetre glissante {s.fenetre_s:.0f} s)",
        "",
        f"  RECEPTION      {s.rx_hz:8.1f} msg/s   {s.rx_bps / 1024:7.1f} kio/s",
        f"  PERTE          {s.perte_pct:8.2f} %       "
        f"{s.perdus} manquants sur {s.recus + s.perdus} attendus",
        f"  EMISSION       {s.tx_hz:8.1f} msg/s   {s.tx_bps / 1024:7.1f} kio/s",
        f"  LATENCE        p50 {ms(s.latence_p50_ms):>10}     p95 {ms(s.latence_p95_ms)}",
        "",
        f"  plus grand silence en emission . {s.tx_trou_max_s} s"
        + ("   <-- DEPASSE GUID_TIMEOUT" if s.tx_trou_max_s > GUID_TIMEOUT else ""),
        f"  sauts de sequence non credibles  {s.desordres}"
        "   (doublons, reordonnancements, redemarrages)",
        "",
        f"  {'emetteur':<26} {'Hz':>6}",
    ]
    lignes += [f"  {src:<26} {hz:>6.1f}" for src, hz in s.par_source] or ["  —"]
    lignes += ["", f"  {'message':<26} {'Hz':>6}"]
    lignes += [f"  {t:<26} {hz:>6.1f}" for t, hz in s.par_message]
    return "\n".join(lignes)


@app.get("/degrade")
def degrade(perte: float = None):
    """Robinet de dégradation : jette une fraction des messages reçus.

    Sert à deux choses. **Étalonner l'instrument** — on demande 10 %, le compteur
    de pertes doit lire 10 %, sinon il ne mesure pas ce qu'il prétend. Et
    **observer le système sous liaison dégradée** sans avoir besoin d'une vraie
    radio ni de s'éloigner : c'est le banc que réclame la phase 2 du swarm.

    N'agit que sur la réception. Simuler la perte montante demanderait de ne pas
    émettre, ce que fait déjà `/cut` — en tout ou rien plutôt qu'en proportion.
    """
    with _lock:
        if perte is not None:
            _degrade["perte"] = max(0.0, min(0.95, perte))
        return dict(_degrade)


@app.get("/cut")
def cut(ms: int = 800, roll: float = 1.0, fwd: float = 0.0, pre: float = 1.5):
    """Sonde de coupure (PORTFOLIO §1.5-D) : cesse d'émettre pendant `ms` ms.

    Ne pas confondre avec « envoyer une commande nulle » : envoyer des zéros,
    c'est encore parler, et le firmware continue de nous croire vivants. Ici on
    se tait vraiment, et on laisse `GUID_TIMEOUT` expirer.

    **La sonde pose elle-même sa condition d'expérience.** Couper alors que le
    drone est déjà à plat ne montre rien : il n'y a rien à remettre à plat, et on
    ne peut pas distinguer « ArduPilot a réagi » de « il n'y avait rien à faire ».
    Donc on impose d'abord une inclinaison franche pendant `pre` secondes, **et on
    maintient la même intention pendant tout l'enregistrement**. L'intention étant
    constante, tout ce qui bouge dans la trace vient du firmware, pas de nous.

    Signature attendue : le drone tient l'inclinaison -> silence -> il la tient
    encore pendant `GUID_TIMEOUT` -> il se remet à plat tout seul -> on reparle
    -> il y retourne. Un plateau, une marche, un plateau.

    Par défaut on incline en ROULIS : la garde de proximité peut annuler un piqué
    (c'est son travail), ce qui fausserait la mesure. Le roulis, lui, n'est jamais
    bridé.
    """
    ms = max(100, min(5000, ms))
    pre = max(0.5, min(5.0, pre))
    now = time.time()
    duree = pre + ms / 1000.0 + CUT_TAIL
    with _lock:
        if not _drone["flying"]:
            return {"erreur": "pas en vol"}
        # L'intention opérateur couvre TOUT l'enregistrement (avant, pendant, après).
        _manual.update({"fwd": fwd, "right": roll, "up": 0.0, "until": now + duree})
        _cut.update({"t0": now, "silence": now + pre,
                     "until": now + pre + ms / 1000.0, "end": now + duree,
                     "ms": ms, "trace": [], "running": True})
    # FREINAGE. Incliner pendant 5 s, c'est ACCÉLÉRER pendant 5 s ; se remettre à
    # plat ensuite ne freine rien (pas de retour de vitesse, cf. §1.1) et le drone
    # s'en va indéfiniment. Un outil de diagnostic qui laisse le véhicule dans un
    # état pire qu'il ne l'a trouvé n'est pas un outil de diagnostic. On rend donc
    # la même inclinaison en sens inverse, pendant la même durée.
    threading.Thread(target=_cut_freinage, args=(duree, roll, fwd), daemon=True).start()
    return {"coupure_ms": ms, "guid_timeout_s": GUID_TIMEOUT,
            "inclinaison_imposee": {"roulis": roll, "avant": fwd},
            "pre_roll_s": pre, "freinage_apres_s": duree,
            "relire": f"/cut/trace dans {duree:.1f} s (le freinage suit)"}


def _cut_freinage(duree, roll, fwd):
    """Ramène le drone au repos SANS GPS, par comptabilité de quantité de mouvement.

    On ne mesure jamais la vitesse. On sait juste ce qu'on a fait pour la créer :

        Δv = g · ∫ tan(inclinaison) dt

    L'inclinaison vient de l'IMU. Donc on intègre ce qu'on a pris pendant le tir,
    puis on rend **la même intégrale en sens inverse**. C'est la même comptabilité
    qu'une centrale inertielle, sur un seul axe et sur quelques secondes.

    Pourquoi pas la vitesse GPS, qui serait plus précise : **cette sonde doit
    pouvoir tourner sur le vrai drone, en GNSS-denied.** Un nettoyage qui exige le
    GPS rend la procédure de test injouable dans l'environnement même pour lequel
    le système est conçu. Précision perdue, transférabilité gagnée.

    Deux versions précédentes, toutes deux fausses et instructives :
      1. rendre la même inclinaison pendant la même durée -> sur-corrige, parce que
         la phase d'accélération perd du temps que le freinage n'a pas (rampe
         initiale, mise à plat pendant une partie du silence, remontée ensuite).
         Signature : dérive inversée, plus rapide quand le silence est plus long.
      2. boucle fermée sur la vitesse GPS -> juste, mais non transférable.

    Reste imparfait : la traînée n'est pas modélisée, et l'intégrale est
    échantillonnée à la cadence de la télémétrie d'attitude. On vise l'ordre de
    grandeur, pas le zéro.
    """
    pas = 0.05
    ix = iy = 0.0                      # ∫tan(pitch)dt et ∫tan(roll)dt, en secondes

    def integre(t_prec):
        nonlocal ix, iy
        now = time.time()
        dt = now - t_prec
        with _lock:
            r, p = math.radians(_drone["roll"]), math.radians(_drone["pitch"])
        iy += math.tan(r) * dt         # droite  (roulis)
        ix += math.tan(p) * dt         # arrière (tangage positif = nez en haut)
        return now

    # Phase 1 : on regarde le tir se dérouler et on compte ce qu'il accumule.
    t = time.time()
    fin = t + duree
    while time.time() < fin:
        time.sleep(pas)
        t = integre(t)

    # Phase 2 : on rend l'intégrale. Le gain sature l'intention tant que la dette
    # est grande, puis la relâche tout seul près de zéro — donc pas de dépassement.
    fin = time.time() + 6 * duree      # garde-fou
    while time.time() < fin:
        with _lock:
            if not _drone["flying"]:
                break
        if abs(ix) < 0.02 and abs(iy) < 0.02:
            break
        with _lock:
            _manual.update({"fwd": max(-1.0, min(1.0, 30.0 * ix)),
                            "right": max(-1.0, min(1.0, -30.0 * iy)),
                            "up": 0.0, "until": time.time() + 0.5})
        time.sleep(pas)
        t = integre(t)
    with _lock:
        _manual["until"] = 0.0         # on rend la main à la loi


def _cut_resume():
    """Le résumé de la dernière coupure. Séparé du rendu pour servir les deux formats."""
    with _lock:
        trace, ms, en_cours = list(_cut["trace"]), _cut["ms"], _cut["running"]
    if not trace:
        return None, []
    muet = [p for p in trace if not p["emis"]]
    avant = [p for p in trace if p["emis"] and (not muet or p["t"] < muet[0]["t"])]
    apres = [p for p in trace if p["emis"] and muet and p["t"] > muet[-1]["t"]]
    alts = [p["alt"] for p in trace]

    def incl(p):                       # inclinaison réelle, tous axes confondus
        return max(abs(p["roll"]), abs(p["pitch"]))

    # L'assiette au moment EXACT où l'on s'est tu. Si elle est faible, la mesure
    # ne vaut rien : il n'y avait rien à remettre à plat.
    depart = incl(avant[-1]) if avant else (incl(muet[0]) if muet else 0.0)
    valide = depart >= 3.0

    # Le compte à rebours de GUID_TIMEOUT part du DERNIER MESSAGE REÇU côté
    # firmware, pas du premier échantillon silencieux. C'est de là qu'on mesure.
    t_dernier = avant[-1]["t"] if avant else (muet[0]["t"] if muet else 0.0)

    # « Lâcher » = s'écarter de plus de 1° de l'assiette qu'il tenait. Chercher une
    # division par deux serait trop grossier : si le silence dure à peine plus que
    # le délai, la chute n'a pas le temps d'aller si loin, et on conclurait à tort
    # qu'il ne s'est rien passé.
    lache = next((round(p["t"] - t_dernier, 2) for p in muet
                  if abs(incl(p) - depart) > 1.0), None)
    # La mise à plat se poursuit souvent APRÈS la reprise (le firmware a déjà
    # basculé sa consigne) : on cherche donc le minimum sur toute la suite.
    suite = [p for p in trace if p["t"] >= t_dernier]
    mini = min((incl(p) for p in suite), default=None)

    # Le plus grand trou entre deux commandes réellement émises. S'il dépasse
    # GUID_TIMEOUT, la boucle déclenche le failsafe toute seule, sans le savoir.
    # On ne compte que des points CONSÉCUTIFS tous deux émis : sinon le silence
    # volontaire serait compté comme un bégaiement, et la mesure dirait n'importe quoi.
    trou = max((b["t"] - a["t"] for a, b in zip(trace, trace[1:])
                if a["emis"] and b["emis"]), default=0.0)

    return {
        "en_cours": en_cours,
        "coupure_ms": ms,
        "guid_timeout_s": GUID_TIMEOUT,
        "points": len(trace),
        "valide": valide,
        "inclinaison_tenue": round(depart, 1),
        "lachee_apres_s": lache,
        "inclinaison_mini_atteinte": None if mini is None else round(mini, 1),
        "inclinaison_finale": round(incl(trace[-1]), 1),
        "derive_altitude_m": round(max(alts) - min(alts), 1),
        "modes_vus": sorted({p["mode"] for p in trace}),
        "reprise_ok": bool(apres),
        "plus_grand_trou_entre_commandes_s": round(trou, 2),
    }, trace


@app.get("/cut/trace", response_class=PlainTextResponse)
def cut_trace(json: int = 0):
    """La trace de la dernière coupure. Texte aligné par défaut (lisible dans un
    navigateur), `?json=1` pour la structure brute."""
    r, trace = _cut_resume()
    if r is None:
        return "aucune coupure enregistrée — lance /cut?ms=800 pendant un vol"
    if json:
        return JSONResponse({**r, "trace": trace})

    def duree(v):
        return "jamais" if v is None else f"{v:.2f} s"

    verdict = ("MESURE VALIDE" if r["valide"] else
               "MESURE VIDE — le drone etait deja a plat, rien a observer")
    tete = [
        f"SONDE DE COUPURE — silence de {r['coupure_ms']} ms   "
        f"(GUID_TIMEOUT = {r['guid_timeout_s']} s)",
        f"  {verdict}",
        f"  inclinaison tenue avant silence . {r['inclinaison_tenue']}°",
        f"  LACHEE apres .................... {duree(r['lachee_apres_s'])}"
        + (f"   <- GUID_TIMEOUT = {r['guid_timeout_s']} s + reponse physique"
           if r['lachee_apres_s'] is not None
           else f"   <- silence trop court : il n'a jamais lache"),
        f"  descend jusqu'a ................. {r['inclinaison_mini_atteinte']}°",
        f"  inclinaison a la fin ............ {r['inclinaison_finale']}°"
        f"   (reprise)",
        f"  derive d'altitude ............... {r['derive_altitude_m']} m",
        f"  modes traverses ................. {', '.join(r['modes_vus'])}",
        f"  reprise apres silence ........... {'oui' if r['reprise_ok'] else 'non'}"
        + ("   (enregistrement en cours)" if r["en_cours"] else ""),
        f"  plus grand trou entre commandes . {r['plus_grand_trou_entre_commandes_s']} s"
        + ("   <-- DEPASSE GUID_TIMEOUT : la boucle declenche le failsafe seule"
           if r["plus_grand_trou_entre_commandes_s"] > r["guid_timeout_s"] else ""),
        "",
        f"{'t':>6} {'emis':>5} {'roll_cmd':>9} {'roll':>7} "
        f"{'pitch_cmd':>10} {'pitch':>7} {'alt':>6}  mode",
    ]
    lignes = []
    for p in trace:
        rc = "  —  " if p["roll_cmd"] is None else f"{p['roll_cmd']:+.1f}"
        pc = "  —  " if p["pitch_cmd"] is None else f"{p['pitch_cmd']:+.1f}"
        lignes.append(f"{p['t']:6.2f} {'oui' if p['emis'] else 'NON':>5} "
                      f"{rc:>9} {p['roll']:+7.1f} {pc:>10} {p['pitch']:+7.1f} "
                      f"{p['alt']:6.1f}  {p['mode']}")
    return "\n".join(tete + lignes)


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
  #dpad{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin:6px 0}
  #dpad button{padding:8px 4px;border-radius:6px;border:1px solid #233040;background:#161d26;
    color:#bcccdb;font-size:12px;cursor:pointer}
  #dpad button:hover{border-color:#5ec8ff;color:#fff}
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
    <div class="stat"><span>Taille bbox</span><b id="sz">0</b></div>
    <div id="bar"><div id="barfill" style="left:50%"></div></div>
    <button class="src" onclick="engage()">ENGAGER le suivi</button>
    <button class="src" onclick="disengage()">Désengager</button>
    <button class="src" onclick="unlock()">Unlock</button>
    <div class="lbl">DRONE — GUIDED_NOGPS</div>
    <button class="src" onclick="takeoff()">Décoller + activer Mode B</button>
    <div class="stat"><span>État</span><b id="dst" style="font-size:11px">déconnecté</b></div>
    <div class="stat"><span>Mode</span><b id="dmod" style="font-size:12px">–</b></div>
    <div class="stat"><span>Cap</span><b id="dhdg">–</b></div>
    <div class="stat"><span>Alt</span><b id="dalt">–</b></div>
    <div class="lbl">COMMANDE ÉMISE — porte de sortie</div>
    <div class="stat"><span>Émetteur</span><b id="csrc" style="font-size:12px">idle</b></div>
    <div class="stat"><span>Roulis</span><b id="croll">0°</b></div>
    <div class="stat"><span>Tangage</span><b id="cpit">0°</b></div>
    <div class="stat"><span>Δcap</span><b id="cyaw">0°</b></div>
    <div class="stat"><span>Poussée</span><b id="cthr">0.50</b></div>
    <div class="stat"><span>Garde</span><b id="cgrd" style="font-size:11px">—</b></div>
    <div class="lbl">PILOTAGE MANUEL</div>
    <div id="dpad">
      <button onclick="fly(0,0,1)">Monter</button>
      <button onclick="fly(1,0,0)">Avancer</button>
      <button onclick="fly(0,0,-1)">Descendre</button>
      <button onclick="fly(0,-1,0)">Gauche</button>
      <button onclick="fly(-1,0,0)">Reculer</button>
      <button onclick="fly(0,1,0)">Droite</button>
    </div>
    <div class="lbl">LIAISON — <a href="/link" target="_blank" style="color:#5ec8ff">détail</a></div>
    <div class="stat"><span>Reçu</span><b id="lrx">–</b></div>
    <div class="stat"><span>Perte</span><b id="lloss">–</b></div>
    <div class="stat"><span>Débit</span><b id="lbps">–</b></div>
    <div class="stat"><span>Latence p50</span><b id="llat">–</b></div>
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
async function fly(fwd,right,up){ await fetch(`/fly?fwd=${fwd}&right=${right}&up=${up}&dur=1.2`); }
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
    lk.textContent=t.locked?(t.has?(t.found?'TRACK':'coast'):'perdu'):'—';
    eng.textContent=t.engage?'ENGAGÉ':'—';
    eng.style.color=t.engage?'#ff5a4d':'#fff';
    er.textContent=(t.error??0).toFixed(2);
    sz.textContent=(t.size??0).toFixed(2);
    const ds=await (await fetch('/drone/status')).json();
    dst.textContent=ds.status; dmod.textContent=ds.mode??'–';
    dmod.style.color=(ds.mode==='GUIDED_NOGPS')?'#5ec8ff':'#ff5a4d';
    dhdg.textContent=(ds.hdg??0)+'°'; dalt.textContent=(ds.alt??0)+' m';
    const L=await (await fetch('/link?json=1')).json();
    lrx.textContent=(L.rx_hz??0).toFixed(0)+' msg/s';
    lloss.textContent=(L.perte_pct??0).toFixed(2)+' %';
    lloss.style.color=(L.perte_pct>1)?'#ff5a4d':'#fff';
    lbps.textContent=((L.rx_bps||0)/1024).toFixed(1)+' kio/s';
    llat.textContent=(L.latence_p50_ms==null)?'–':L.latence_p50_ms.toFixed(0)+' ms';
    const c=await (await fetch('/command')).json();
    csrc.textContent=c.src; croll.textContent=(c.roll??0).toFixed(1)+'°';
    cpit.textContent=(c.pitch??0).toFixed(1)+'°'; cyaw.textContent=(c.dyaw??0).toFixed(1)+'°';
    cthr.textContent=(c.thrust??0.5).toFixed(2);
    cgrd.textContent=(c.reasons&&c.reasons.length)?c.reasons.join(' · '):'—';
    cgrd.style.color=(c.reasons&&c.reasons.length)?'#ff5a4d':'#fff';
    // la barre suit le roulis commandé (±15° = les bornes dures de la porte)
    barfill.style.left=Math.max(0,Math.min(100,50+(c.roll||0)/15*50))+'%';
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
