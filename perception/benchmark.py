"""benchmark.py — mAP + latence (p50/p95) + FPS par précision (RTX 4060).

Compare PyTorch FP32 (best.pt) vs TensorRT FP32 / FP16 / INT8. Sort un tableau
Markdown (stdout + benchmark.md). Prérequis : make export. Lance : make bench
"""
from pathlib import Path

import numpy as np
import yaml
from ultralytics import YOLO

HERE = Path(__file__).resolve().parent
DATA = HERE / "argos_visdrone.yaml"
ENG = HERE / "engines"
IMGSZ = 640
WARMUP = 20
N_LAT = 200


def find_best():
    cands = sorted((HERE.parent / "runs").rglob("weights/best.pt"),
                   key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def val_image():
    cfg = yaml.safe_load(DATA.read_text())
    return next((Path(cfg["path"]) / cfg["val"]).glob("*.jpg"))


def latency_ms(model, img):
    """Latence d'inférence pure (ms), warmup exclu, p50/p95 sur N_LAT runs."""
    for _ in range(WARMUP):
        model.predict(img, imgsz=IMGSZ, device=0, verbose=False)
    t = []
    for _ in range(N_LAT):
        r = model.predict(img, imgsz=IMGSZ, device=0, verbose=False)
        t.append(r[0].speed["inference"])      # ms (Ultralytics synchronise le GPU)
    a = np.array(t)
    return float(np.percentile(a, 50)), float(np.percentile(a, 95))


def main():
    best = find_best()
    variants = [
        ("PyTorch FP32", best),
        ("TensorRT FP32", ENG / "best_fp32.engine"),
        ("TensorRT FP16", ENG / "best_fp16.engine"),
        ("TensorRT INT8", ENG / "best_int8.engine"),
    ]
    img = val_image()
    rows = []
    for name, path in variants:
        if path is None or not Path(path).exists():
            print(f"SKIP {name} (absent)")
            continue
        print(f"== {name} ==")
        model = YOLO(str(path))
        m = model.val(data=str(DATA), imgsz=IMGSZ, batch=1, device=0, verbose=False)
        p50, p95 = latency_ms(model, img)
        rows.append((name, m.box.map50, m.box.map, p50, p95, 1000.0 / p50))

    lines = [
        "| Précision | mAP50 | mAP50-95 | latence p50 (ms) | p95 (ms) | FPS |",
        "|---|---|---|---|---|---|",
    ]
    for name, m50, m, p50, p95, fps in rows:
        lines.append(f"| {name} | {m50:.3f} | {m:.3f} | {p50:.2f} | {p95:.2f} | {fps:.0f} |")
    table = "\n".join(lines)
    print("\n" + table)
    (HERE / "benchmark.md").write_text(
        "# ARGOS perception benchmark — VisDrone 2-class, RTX 4060\n\n" + table + "\n")
    print("\nÉcrit : benchmark.md")


if __name__ == "__main__":
    main()
