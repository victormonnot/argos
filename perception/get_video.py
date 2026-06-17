"""get_video.py — récupère assets/drone.mp4 pour la démo Mode A.

Priorité : une VRAIE vidéo aérienne continue (Pexels, libre d'usage, sans
attribution) — lisible pour un humain et pile dans le domaine du détecteur.
Fallback : une vidéo fabriquée depuis les images VisDrone si le download échoue.

Pour utiliser ton propre clip : remplace assets/drone.mp4, ou lance la console
avec ARGOS_SOURCE=/chemin/vers/ton.mp4
"""
import urllib.request
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
OUT = HERE / "assets" / "drone.mp4"
# Clip aérien "moving vehicles" (Pexels, free to use). Remplaçable.
PEXELS_URL = "https://www.pexels.com/download/video/7005467/"
VAL = HERE.parent.parent / "datasets" / "VisDrone" / "images" / "val"


def download_real():
    OUT.parent.mkdir(exist_ok=True)
    req = urllib.request.Request(PEXELS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(OUT, "wb") as f:
        f.write(r.read())
    if OUT.stat().st_size > 1_000_000:
        return True
    OUT.unlink(missing_ok=True)
    return False


def synth_from_visdrone():
    imgs = sorted(VAL.glob("*.jpg"))[:200]
    if not imgs:
        raise SystemExit("Ni download possible ni images VisDrone — lance `make data`.")
    OUT.parent.mkdir(exist_ok=True)
    vw = cv2.VideoWriter(str(OUT), cv2.VideoWriter_fourcc(*"mp4v"), 8, (960, 540))
    for p in imgs:
        im = cv2.imread(str(p))
        if im is not None:
            vw.write(cv2.resize(im, (960, 540)))
    vw.release()


if __name__ == "__main__":
    ok = False
    try:
        ok = download_real()
    except Exception as e:
        print("Download Pexels échoué:", e)
    if ok:
        print(f"✅ vraie vidéo aérienne -> {OUT} ({OUT.stat().st_size // 1024 // 1024} Mo)")
    else:
        print("→ fallback : synthèse depuis les images VisDrone")
        synth_from_visdrone()
        print(f"Écrit (fallback) : {OUT}")
