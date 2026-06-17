# ARGOS perception benchmark — VisDrone 2-class, RTX 4060

| Précision | mAP50 | mAP50-95 | latence p50 (ms) | p95 (ms) | FPS |
|---|---|---|---|---|---|
| PyTorch FP32 | 0.527 | 0.278 | 6.57 | 15.38 | 152 |
| TensorRT FP32 | 0.526 | 0.278 | 5.45 | 6.62 | 184 |
| TensorRT FP16 | 0.527 | 0.277 | 5.04 | 6.17 | 198 |
| TensorRT INT8 | 0.507 | 0.260 | 5.38 | 7.55 | 186 |
