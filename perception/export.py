"""export.py — best.pt -> ONNX + moteurs TensorRT (FP32, FP16, INT8 PTQ).

Range les moteurs sous perception/engines/. L'INT8 est calibré (post-training
quantization) sur VisDrone via argos_visdrone.yaml. Lance : make export
"""
import shutil
from pathlib import Path

from ultralytics import YOLO

HERE = Path(__file__).resolve().parent
DATA = HERE / "argos_visdrone.yaml"
OUT = HERE / "engines"
IMGSZ = 640


def find_best():
    cands = sorted((HERE.parent / "runs").rglob("weights/best.pt"),
                   key=lambda p: p.stat().st_mtime)
    if not cands:
        raise SystemExit("best.pt introuvable — entraîne d'abord (make train).")
    return cands[-1]


def export_engine(tag, **kw):
    """Exporte un moteur TensorRT et le range en engines/best_<tag>.engine."""
    path = Path(YOLO(str(BEST)).export(format="engine", imgsz=IMGSZ, device=0, **kw))
    OUT.mkdir(exist_ok=True)
    dst = OUT / f"best_{tag}.engine"
    shutil.move(str(path), dst)
    print(f"  -> {dst}")


if __name__ == "__main__":
    BEST = find_best()
    print(f"Modèle source : {BEST}")
    OUT.mkdir(exist_ok=True)

    print("== ONNX (intermédiaire portable) ==")
    onnx = YOLO(str(BEST)).export(format="onnx", imgsz=IMGSZ)
    shutil.move(str(onnx), OUT / "best.onnx")

    print("== TensorRT FP32 ==")
    export_engine("fp32")
    print("== TensorRT FP16 ==")
    export_engine("fp16", half=True)
    print("== TensorRT INT8 (PTQ — calibration VisDrone) ==")
    export_engine("int8", int8=True, data=str(DATA))

    print("Moteurs prêts dans engines/. Benchmark : make bench")
