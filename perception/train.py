"""train.py — fine-tune YOLO11n sur VisDrone 2 classes (personne / véhicule).

Prérequis : make data (télécharge VisDrone + génère argos_visdrone.yaml).
Lance : make train
"""
from ultralytics import YOLO

if __name__ == "__main__":
    model = YOLO("yolo11n.pt")          # on part du nano pré-entraîné COCO
    model.train(
        data="argos_visdrone.yaml",
        epochs=50,
        imgsz=640,
        batch=16,                       # 4060 8 Go : descends à 8 si OOM
        device=0,                       # le GPU
        project="runs",
        name="visdrone2",
    )
