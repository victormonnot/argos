"""Pinned YOLOX person inference on camera JPEGs, without model downloads.

The upstream ONNX example defines BGR 0..255 input, top-left letterboxing and
the stride-grid output encoding used here. Only COCO's ``person`` class is
published. A box is a visual measurement, not a person's identity or distance.
See https://github.com/Megvii-BaseDetection/YOLOX/tree/main/demo/ONNXRuntime.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import time
from types import MappingProxyType


MODEL_NAME = "yolox_tiny.onnx"
MODEL_URL = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx"
MODEL_SHA256 = "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7"
MODEL_BYTES = 20_219_662
MODEL_LICENSE_URL = "https://github.com/Megvii-BaseDetection/YOLOX/blob/0.1.1rc0/LICENSE"
INPUT_SIZE = 416
MAX_DETECTIONS = 16
CONFIDENCE_THRESHOLD = .35
NMS_THRESHOLD = .45


@dataclass(frozen=True)
class ModelSpec:
    variant: str
    label: str
    filename: str
    url: str
    sha256: str
    size_bytes: int
    input_size: int


# The legacy MODEL_* / INPUT_SIZE names above remain aliases for the default.
# Only these unmodified official exports are accepted; filenames confer no trust.
MODEL_CATALOG = MappingProxyType({
    "tiny": ModelSpec("tiny", "YOLOX-Tiny", MODEL_NAME, MODEL_URL,
                      MODEL_SHA256, MODEL_BYTES, INPUT_SIZE),
    "s": ModelSpec("s", "YOLOX-S", "yolox_s.onnx",
        "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx",
        "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
        35_858_002, 640),
})


def get_model_spec(variant: str = "tiny") -> ModelSpec:
    if not isinstance(variant, str) or variant not in MODEL_CATALOG:
        raise ValueError("vision variant must be tiny or s")
    return MODEL_CATALOG[variant]


def validate_inference_threads(threads: int) -> int:
    if type(threads) is not int or not 1 <= threads <= 6:
        raise ValueError("vision threads must be an integer between 1 and 6")
    return threads


def default_model_path(variant: str = "tiny") -> Path:
    model = get_model_spec(variant)
    cache = os.environ.get("XDG_CACHE_HOME", "")
    root = Path(cache) if cache and Path(cache).is_absolute() else Path.home() / ".cache"
    return root / "argos" / "models" / model.filename


def read_verified_model(model_path: str | Path, *, variant: str = "tiny") -> bytes:
    """Read the exact pinned model; a missing or changed file is an error."""
    model = get_model_spec(variant)
    path = Path(model_path).expanduser()
    try:
        with path.open("rb") as source:
            data = source.read(model.size_bytes + 1)
    except OSError as exc:
        raise ValueError(
            f"vision model is unavailable; run examples/setup_vision_model.py --variant {variant} first"
        ) from exc
    if len(data) != model.size_bytes or hashlib.sha256(data).hexdigest() != model.sha256:
        raise ValueError("vision model failed its size/SHA-256 check")
    return data


class YoloXPersonDetector:
    """One CPU inference instance, used serially by its owning worker.

    OpenCV is optional and imported only on construction. It defaults to two
    CPU threads; an explicit 1..6 limit is an OpenCV process-wide setting.
    The network is loaded from the
    verified bytes, so a file replacement cannot bypass the checksum check.
    ``inference_ms`` measures network inference, excluding JPEG decode and NMS.
    """

    def __init__(self, model_path: str | Path, *, variant: str = "tiny", threads: int = 2):
        self.threads = validate_inference_threads(threads)
        self.model = get_model_spec(variant)
        self._input_size = self.model.input_size
        data = read_verified_model(model_path, variant=variant)
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("vision requires the optional ARGOS camera dependency") from exc
        self._cv2, self._np = cv2, np
        cv2.setNumThreads(self.threads)
        self._net = cv2.dnn.readNetFromONNX(np.frombuffer(data, dtype=np.uint8))
        self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        # CPU is the OpenCV backend default. The pinned OpenCV 5 graph engine
        # does not support setPreferableTarget and warns even for CPU selection.
        grids, strides = [], []
        for stride in (8, 16, 32):
            yy, xx = np.mgrid[:self._input_size // stride, :self._input_size // stride]
            grids.append(np.column_stack((xx.ravel(), yy.ravel())))
            strides.append(np.full((xx.size, 1), stride))
        self._grid = np.concatenate(grids)
        self._strides = np.concatenate(strides)

    def detect(self, jpeg: bytes) -> dict:
        if not isinstance(jpeg, bytes) or not jpeg or len(jpeg) > 16 * 1024 * 1024:
            raise ValueError("vision input must be a nonempty camera JPEG of at most 16 MiB")
        cv2, np = self._cv2, self._np
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("vision could not decode the camera JPEG")
        height, width = image.shape[:2]
        if not (0 < width <= 4096 and 0 < height <= 4096):
            raise ValueError("vision image dimensions must be between 1 and 4096 pixels")
        ratio = min(self._input_size / width, self._input_size / height)
        scaled_width, scaled_height = max(1, int(width * ratio)), max(1, int(height * ratio))
        resized = cv2.resize(image, (scaled_width, scaled_height), interpolation=cv2.INTER_LINEAR)
        padded = np.full((self._input_size, self._input_size, 3), 114, dtype=np.uint8)
        padded[:scaled_height, :scaled_width] = resized
        blob = np.ascontiguousarray(padded.transpose(2, 0, 1)[None], dtype=np.float32)
        started = time.perf_counter()
        self._net.setInput(blob)
        output = self._net.forward()
        inference_ms = (time.perf_counter() - started) * 1000
        detections = self._decode(output, ratio, width, height)
        return {
            "width": int(width), "height": int(height), "detections": detections,
            "inference_ms": inference_ms,
        }

    def _decode(self, output, ratio: float, width: int, height: int) -> list[dict]:
        np = self._np
        if output.shape != (1, len(self._grid), 85) or not np.isfinite(output).all():
            raise RuntimeError("vision model returned an invalid output tensor")
        rows = output[0]
        scores = rows[:, 4] * rows[:, 5]
        indices = np.flatnonzero((scores >= CONFIDENCE_THRESHOLD) & (scores <= 1))
        if not len(indices):
            return []
        centres = (rows[indices, :2] + self._grid[indices]) * self._strides[indices]
        with np.errstate(over="ignore", invalid="ignore"):
            sizes = np.exp(rows[indices, 2:4]) * self._strides[indices]
        if not np.isfinite(sizes).all():
            raise RuntimeError("vision model returned invalid box dimensions")
        corners = np.column_stack((centres - sizes / 2, centres + sizes / 2)) / ratio
        corners[:, (0, 2)] = np.clip(corners[:, (0, 2)], 0, width)
        corners[:, (1, 3)] = np.clip(corners[:, (1, 3)], 0, height)
        areas = (corners[:, 2] - corners[:, 0]) * (corners[:, 3] - corners[:, 1])
        # Stable score order makes ties deterministic. NMS operates only on people.
        order = np.argsort(-scores[indices], kind="stable")
        order = order[areas[order] > 0]
        selected = []
        while len(order) and len(selected) < MAX_DETECTIONS:
            current, rest = int(order[0]), order[1:]
            selected.append(current)
            overlap_size = np.maximum(
                0, np.minimum(corners[current, 2:], corners[rest, 2:])
                - np.maximum(corners[current, :2], corners[rest, :2]),
            )
            intersection = overlap_size[:, 0] * overlap_size[:, 1]
            union = areas[current] + areas[rest] - intersection
            order = rest[intersection / union <= NMS_THRESHOLD]
        result = []
        for index in selected:
            left, top, right, bottom = corners[index]
            result.append({
                "box": [float(left / width), float(top / height),
                        float((right - left) / width), float((bottom - top) / height)],
                "confidence": float(scores[indices[index]]),
            })
        return result
