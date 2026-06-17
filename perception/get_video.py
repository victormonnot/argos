"""get_video.py — récupère les vidéos de démo Mode A (Pexels, libres d'usage).

3 clips : trafic top-down, piétons top-down, fly-through de rue.
Fallback : si un download échoue, on synthétise ce slot depuis les images VisDrone.
Pour ton propre clip : dépose n'importe quel .mp4 dans assets/ (cf. VIDEOS dans console.py).
"""
import urllib.request
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
VAL = HERE.parent.parent / "datasets" / "VisDrone" / "images" / "val"

# fichier -> id Pexels (clips free-to-use, sans attribution)
CLIPS = {
    "vehicles.mp4": "7005467",
    "people.mp4": "15325960",
    "fpv.mp4": "8782920",
}


def download(pid, out):
    req = urllib.request.Request(f"https://www.pexels.com/download/video/{pid}/",
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(out, "wb") as f:
        f.write(r.read())
    return out.stat().st_size > 1_000_000


def synth(out):
    imgs = sorted(VAL.glob("*.jpg"))[:200]
    if not imgs:
        return
    vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), 8, (960, 540))
    for p in imgs:
        im = cv2.imread(str(p))
        if im is not None:
            vw.write(cv2.resize(im, (960, 540)))
    vw.release()


if __name__ == "__main__":
    ASSETS.mkdir(exist_ok=True)
    for fname, pid in CLIPS.items():
        out = ASSETS / fname
        ok = False
        try:
            ok = download(pid, out)
        except Exception as e:
            print(f"{fname}: download échoué ({e})")
        if ok:
            print(f"✅ {fname}  ({out.stat().st_size // 1024 // 1024} Mo)")
        else:
            out.unlink(missing_ok=True)
            print(f"→ {fname}: fallback VisDrone")
            synth(out)
