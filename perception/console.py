"""console.py — Mode A : console opérateur web (overlay de détection sur flux vidéo).

Cœur : source vidéo -> inférence TensorRT FP16 -> HUD (OpenCV) -> stream MJPEG.
Affichage : navigateur, zéro dépendance display (parfait en SSH/WSL).
Ouvre http://localhost:8088  (ou http://<ip-tailscale-du-fixe>:8088 depuis le Mac).
Port configurable via ARGOS_PORT.

Lance : make console
Source : assets/drone.mp4 par défaut ; override avec ARGOS_SOURCE=... (fichier ou n° de webcam).
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
ENGINE = HERE / "engines" / "best_fp16.engine"     # le sweet spot du benchmark
SOURCE = os.environ.get("ARGOS_SOURCE", str(HERE / "assets" / "drone.mp4"))
IMGSZ = 640
CONF = 0.25
COLORS = {0: (0, 200, 0), 1: (0, 140, 255)}          # BGR : personne=vert, véhicule=orange

# État partagé, alimenté par le thread worker (l'inférence tourne 1 seule fois,
# quel que soit le nombre de clients connectés).
_state = {"jpeg": None, "dets": [], "fps": 0.0}
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────
#  annotate() — LE CANVAS DE VICTOR. Tout le look du HUD se règle ici.
#  Raffine librement : couleurs, typo, éléments composés, reticule de lock...
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


def worker():
    """Boucle d'inférence : lit la source, détecte, annote, publie l'état."""
    model = YOLO(str(ENGINE), task="detect")
    names = model.names
    src = int(SOURCE) if SOURCE.isdigit() else SOURCE
    while True:
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            print(f"[console] source illisible: {SOURCE}")
            time.sleep(2)
            continue
        t_prev = time.time()
        while True:
            ok, frame = cap.read()
            if not ok:
                break                                  # fin de vidéo -> on reboucle
            result = model.predict(frame, imgsz=IMGSZ, conf=CONF, device=0, verbose=False)[0]
            now = time.time()
            fps = 1.0 / max(now - t_prev, 1e-6)
            t_prev = now
            annotated, dets = annotate(frame, result, names, fps)
            ok2, buf = cv2.imencode(".jpg", annotated)
            if ok2:
                with _lock:
                    _state["jpeg"] = buf.tobytes()
                    _state["dets"] = dets
                    _state["fps"] = fps
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


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>ARGOS — Mode A</title>
<style>
  body{margin:0;background:#0b0f14;color:#cdd6e0;font-family:system-ui,sans-serif;display:flex}
  #video{flex:1;display:flex;align-items:center;justify-content:center;background:#000}
  #video img{max-width:100%;max-height:100vh}
  #panel{width:260px;padding:16px;background:#11161d;border-left:1px solid #1f2733}
  h1{font-size:14px;letter-spacing:.12em;color:#5ec8ff;margin:0 0 14px}
  .stat{font-size:13px;margin:8px 0;color:#9fb0c0;display:flex;justify-content:space-between}
  .stat b{color:#fff;font-size:18px}
  ul{list-style:none;padding:0;margin:12px 0;font-size:12px;max-height:55vh;overflow:auto}
  li{padding:3px 0;color:#7f93a6;border-bottom:1px solid #161d26}
</style></head><body>
  <div id="video"><img src="/stream"></div>
  <div id="panel">
    <h1>ARGOS · MODE A</h1>
    <div class="stat"><span>FPS</span><b id="fps">–</b></div>
    <div class="stat"><span>Personnes</span><b id="np">0</b></div>
    <div class="stat"><span>Véhicules</span><b id="nv">0</b></div>
    <ul id="list"></ul>
  </div>
<script>
async function poll(){
  try{
    const d=await (await fetch('/detections')).json();
    fps.textContent=d.fps;
    const dets=d.detections||[];
    np.textContent=dets.filter(x=>x.cls==='personne').length;
    nv.textContent=dets.filter(x=>x.cls==='vehicule').length;
    list.innerHTML=dets.slice(0,30).map(x=>`<li>${x.cls} · ${x.conf}</li>`).join('');
  }catch(e){}
  setTimeout(poll,500);
}
poll();
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ARGOS_PORT", "8088")))
