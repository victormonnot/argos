# Pipeline perception ARGOS — reproductible (clin d'œil Gamma Ulixes).
# Usage : make setup, make infer, ... (PY = le python du venv dédié)
PY = .venv/bin/python

.PHONY: setup infer data train eval export bench

setup:   ## crée le venv dédié + installe les deps
	/usr/bin/python3 -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -r requirements.txt

infer:   ## smoke test : inférence YOLO sur le GPU
	$(PY) infer.py

data:    ## télécharge VisDrone + remap en 2 classes (~2 Go)
	$(PY) prepare_data.py

train:   ## fine-tune YOLO11n sur VisDrone (personne / véhicule)
	$(PY) train.py

eval:    ## mAP@50 du best.pt sur la validation
	$(PY) -c "from pathlib import Path; from ultralytics import YOLO; p=sorted(Path('../runs').rglob('weights/best.pt'),key=lambda x:x.stat().st_mtime)[-1]; YOLO(str(p)).val(data='argos_visdrone.yaml', imgsz=640, device=0)"

export:  ## export ONNX -> TensorRT FP32 / FP16 / INT8 (PTQ)
	$(PY) export.py

bench:   ## mAP + FPS + latence p50/p95 par précision -> benchmark.md
	$(PY) benchmark.py
