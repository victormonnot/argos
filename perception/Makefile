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

eval:    ## mAP@50 sur la validation
	@echo "TODO S2 : eval mAP"

export:  ## export ONNX -> TensorRT FP32 / FP16 / INT8 (PTQ)
	@echo "TODO S2 : export TensorRT"

bench:   ## FPS + latence inference p50/p95 par précision
	@echo "TODO S2 : benchmark chiffré -> tableau README"
