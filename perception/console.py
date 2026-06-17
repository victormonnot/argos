"""console.py — Mode A/B : console opérateur web (détection + lock de cible + loi de guidage).

Cœur : source vidéo -> inférence TensorRT FP16 -> HUD (OpenCV) -> stream MJPEG.
Mode B (étape 1) : clique une détection -> cible verrouillée -> erreur (décalage au
centre) -> loi proportionnelle -> commande yaw, affichées. (Drone + boucle fermée ensuite.)
Affichage : navigateur (zéro dépendance display). http://localhost:8088

Lance : make console
"""
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from ultralytics import YOLO

HERE = Path(__file__).resolve().parent
ENGINE = HERE / "engines" / "best_fp16.engine"
IMGSZ = 640
CONF = 0.25
COLORS = {0: (0, 200, 0), 1: (0, 140, 255)}          # BGR : personne=vert, véhicule=orange

# Loi de guidage en lacet (même proportionnelle que control_law.hpp, en Python).
KP_YAW = 60.0          # erreur normalisée (-1..1) -> deg/s
MAX_YAW_DPS = 45.0     # saturation

VIDEOS = {
    "vehicles": ("Trafic · top-down", HERE / "assets" / "vehicles.mp4"),
    "people": ("Piétons · top-down", HERE / "assets" / "people.mp4"),
    "fpv": ("Fly-through rue", HERE / "assets" / "fpv.mp4"),
}
DEFAULT = "vehicles"

_state = {"jpeg": None, "dets": [], "fps": 0.0, "dims": None}
_sel = {"name": DEFAULT}
_track = {"locked": False, "cx": 0.0, "cy": 0.0, "error": 0.0, "yaw": 0.0, "has": False}
_lock = threading.Lock()


def yaw_rate_command(error):
    """Loi proportionnelle saturée — port Python de control_law.hpp."""
    return max(-MAX_YAW_DPS, min(MAX_YAW_DPS, KP_YAW * error))


# ─────────────────────────────────────────────────────────────────────────
#  annotate() — LE CANVAS DE VICTOR. Le look du HUD se règle ici.
# ─────────────────────────────────────────────────────────────────────────
def annotate(frame, result, names, fps):
    h, w = frame.shape[:2]
    dets = []
    for b in result.boxes:
        cls = int(b.cls)
        conf = float(b.conf)
        x1, y1, x2, y2 = map(int, b.xyxy[0])
        color = COLORS.get(cls, (200, 200, 200))
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, f"{names[cls]} {conf:.2f}", (x1, max(12, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        dets.append({"cls": names[cls], "conf": round(conf, 2), "box": [x1, y1, x2, y2]})

    n_person = sum(d["cls"] == "personne" for d in dets)
    n_veh = sum(d["cls"] == "vehicule" for d in dets)
    cv2.rectangle(frame, (0, 0), (w, 30), (0, 0, 0), -1)
    cv2.putText(frame,
                f"ARGOS Mode A   personnes {n_person}   vehicules {n_veh}   {fps:.0f} FPS",
                (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return frame, dets


def track_update(frame, dets):
    """Mode B étape 1 : ré-acquiert la cible verrouillée (plus proche détection),
    calcule l'erreur + la commande yaw, dessine le réticule. Met à jour _track."""
    h, w = frame.shape[:2]
    with _lock:
        if not _track["locked"]:
            return
        cx, cy = _track["cx"], _track["cy"]

    # ré-acquisition naïve : la détection dont le centre est le plus proche
    best, bestd = None, 1e18
    for d in dets:
        x1, y1, x2, y2 = d["box"]
        bx, by = (x1 + x2) / 2, (y1 + y2) / 2
        dd = (bx - cx) ** 2 + (by - cy) ** 2
        if dd < bestd:
            best, bestd = (bx, by), dd
    found = best is not None and bestd < (max(w, h) * 0.12) ** 2
    if found:
        cx, cy = best

    error = (cx - w / 2) / (w / 2)          # -1 gauche .. +1 droite
    yaw = yaw_rate_command(error)

    col = (255, 180, 80)
    cv2.line(frame, (w // 2, h // 2), (int(cx), int(cy)), col, 1, cv2.LINE_AA)
    cv2.circle(frame, (int(cx), int(cy)), 20, (0, 0, 255) if found else (130, 130, 130), 2, cv2.LINE_AA)
    cv2.drawMarker(frame, (int(cx), int(cy)), (0, 0, 255), cv2.MARKER_CROSS, 28, 1, cv2.LINE_AA)
    cv2.putText(frame, f"LOCK  err {error:+.2f}  yaw {yaw:+.0f} deg/s",
                (10, h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2, cv2.LINE_AA)

    with _lock:
        _track.update({"cx": cx, "cy": cy, "error": round(error, 3),
                       "yaw": round(yaw, 1), "has": found})


def worker():
    model = YOLO(str(ENGINE), task="detect")
    names = model.names
    while True:
        with _lock:
            name = _sel["name"]
        cap = cv2.VideoCapture(str(VIDEOS[name][1]))
        if not cap.isOpened():
            print(f"[console] source illisible: {VIDEOS[name][1]}")
            time.sleep(2)
            continue
        t_prev = time.time()
        while True:
            with _lock:
                if _sel["name"] != name:
                    break
            ok, frame = cap.read()
            if not ok:
                break
            result = model.predict(frame, imgsz=IMGSZ, conf=CONF, device=0, verbose=False)[0]
            now = time.time()
            fps = 1.0 / max(now - t_prev, 1e-6)
            t_prev = now
            annotated, dets = annotate(frame, result, names, fps)
            track_update(annotated, dets)
            ok2, buf = cv2.imencode(".jpg", annotated)
            if ok2:
                with _lock:
                    _state["jpeg"] = buf.tobytes()
                    _state["dets"] = dets
                    _state["fps"] = fps
                    _state["dims"] = (frame.shape[1], frame.shape[0])
        cap.release()


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
    if not dims or not dets:
        return {"locked": False}
    w, h = dims
    tx, ty = fx * w, fy * h
    best, bestd = None, 1e18
    for d in dets:
        x1, y1, x2, y2 = d["box"]
        bx, by = (x1 + x2) / 2, (y1 + y2) / 2
        dd = (bx - tx) ** 2 + (by - ty) ** 2
        if dd < bestd:
            best, bestd = (bx, by), dd
    if best:
        with _lock:
            _track.update({"locked": True, "cx": best[0], "cy": best[1]})
    return {"locked": True}


@app.get("/unlock")
def unlock():
    with _lock:
        _track["locked"] = False
    return {"locked": False}


@app.get("/track")
def track():
    with _lock:
        return {k: _track[k] for k in ("locked", "error", "yaw", "has")}


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>ARGOS — Mode A/B</title>
<style>
  body{margin:0;background:#0b0f14;color:#cdd6e0;font-family:system-ui,sans-serif;display:flex}
  #video{flex:1;display:flex;align-items:center;justify-content:center;background:#000}
  #video img{max-width:100%;max-height:100vh;cursor:crosshair}
  #panel{width:268px;padding:16px;background:#11161d;border-left:1px solid #1f2733}
  h1{font-size:14px;letter-spacing:.12em;color:#5ec8ff;margin:0 0 14px}
  .lbl{font-size:11px;letter-spacing:.08em;color:#5b6b7c;margin:14px 0 6px}
  .stat{font-size:13px;margin:7px 0;color:#9fb0c0;display:flex;justify-content:space-between}
  .stat b{color:#fff;font-size:17px}
  button.src{display:block;width:100%;text-align:left;margin:5px 0;padding:9px 11px;border-radius:7px;
    border:1px solid #233040;background:#161d26;color:#bcccdb;font-size:13px;cursor:pointer}
  button.src.on{border-color:#5ec8ff;background:#10212e;color:#fff}
  #bar{height:8px;background:#1a2430;border-radius:4px;margin:8px 0;position:relative}
  #barfill{position:absolute;top:-2px;bottom:-2px;width:3px;background:#ff5a4d;border-radius:2px}
  ul{list-style:none;padding:0;margin:8px 0;font-size:12px;max-height:24vh;overflow:auto}
  li{padding:3px 0;color:#7f93a6;border-bottom:1px solid #161d26}
</style></head><body>
  <div id="video"><img id="cam" src="/stream"></div>
  <div id="panel">
    <h1>ARGOS · MODE A/B</h1>
    <div class="lbl">SOURCE</div>
    <div id="menu"></div>
    <div class="lbl">CIBLE — clique une boîte</div>
    <div class="stat"><span>Lock</span><b id="lk">—</b></div>
    <div class="stat"><span>Erreur</span><b id="er">0</b></div>
    <div class="stat"><span>Yaw cmd</span><b id="yw">0°/s</b></div>
    <div id="bar"><div id="barfill" style="left:50%"></div></div>
    <button class="src" onclick="unlock()">Unlock</button>
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
    list.innerHTML=dets.slice(0,24).map(x=>`<li>${x.cls} · ${x.conf}</li>`).join('');
    const t=await (await fetch('/track')).json();
    lk.textContent=t.locked?(t.has?'ACTIF':'perdu'):'—';
    er.textContent=(t.error??0).toFixed(2);
    yw.textContent=(t.yaw??0).toFixed(0)+'°/s';
    barfill.style.left=Math.max(0,Math.min(100,50+(t.yaw||0)/45*50))+'%';
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
