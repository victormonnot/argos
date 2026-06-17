"""get_video.py — fabrique assets/drone.mp4 depuis les images VisDrone val.

Pourquoi pas un vrai clip aérien : le téléchargement de clips CC s'est avéré peu
fiable, et un clip générique non-aérien ne déclencherait aucune détection. Les
images VisDrone = exactement le domaine d'entraînement -> le détecteur s'allume.
Pour utiliser un vrai clip : remplace simplement assets/drone.mp4 par ton .mp4.
"""
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
VAL = HERE.parent.parent / "datasets" / "VisDrone" / "images" / "val"
OUT = HERE / "assets" / "drone.mp4"
W, H, FPS, N = 960, 540, 8, 200      # 200 frames @ 8 fps ~= 25 s

if __name__ == "__main__":
    imgs = sorted(VAL.glob("*.jpg"))[:N]
    if not imgs:
        raise SystemExit(f"Pas d'images VisDrone dans {VAL} (lance `make data`).")
    OUT.parent.mkdir(exist_ok=True)
    vw = cv2.VideoWriter(str(OUT), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for p in imgs:
        im = cv2.imread(str(p))
        if im is not None:
            vw.write(cv2.resize(im, (W, H)))
    vw.release()
    print(f"Écrit : {OUT}  ({len(imgs)} frames, {W}x{H} @ {FPS} fps)")
