"""infer.py — smoke test : YOLO sur le GPU.

But : prouver que la chaîne perception tourne de bout en bout sur ton 4060
(modèle chargé, inférence sur GPU, détections en sortie). C'est la fondation ;
le fine-tune VisDrone, l'export ONNX→TensorRT et le benchmark viendront dessus.
"""
import time

from ultralytics import YOLO

# yolo11n = la version "nano" (la plus légère), pré-entraînée sur COCO.
# Téléchargée automatiquement au premier run.
model = YOLO("yolo11n.pt")

# device=0 => 1er GPU CUDA (ton 4060). Image d'exemple Ultralytics.
img = "https://ultralytics.com/images/bus.jpg"

t0 = time.perf_counter()
results = model.predict(img, device=0, verbose=False)
dt_ms = (time.perf_counter() - t0) * 1000.0

r = results[0]
names = r.names
classes = [names[int(c)] for c in r.boxes.cls]
print(f"Inference sur : {r.boxes.xyxy.device}")
print(f"{len(classes)} objets détectés : {classes}")
print(f"Temps (1er run, inclut warmup) : {dt_ms:.0f} ms")

r.save("runs/baseline_bus.jpg")
print("Image annotée : perception/runs/baseline_bus.jpg")
