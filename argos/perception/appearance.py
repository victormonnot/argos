"""Bounded crop appearance evidence, not person re-identification.

Encoding runs in the optional vision worker. Validation and similarity use only
the standard library so the event-loop owner never imports OpenCV. Image/source
context and accepted-frame timestamps belong to the caller, not this encoder.
"""
from __future__ import annotations

import math
from numbers import Real


DESCRIPTOR_SIZE = 208
MIN_SIMILARITY = .95
MIN_MARGIN = .08
GROSS_CONTRADICTION = .75
MAX_APPEARANCE_AGE = .35
MAX_DETECTIONS = 16


def _number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def validate_descriptor(value) -> tuple[float, ...] | None:
    """Copy one finite, nonnegative unit vector or an unavailable descriptor."""
    if value is None:
        return None
    if (not isinstance(value, (tuple, list)) or len(value) != DESCRIPTOR_SIZE
            or not all(_number(item) and 0 <= item <= 1 for item in value)):
        raise ValueError("appearance needs 208 finite values between zero and one")
    result = tuple(float(item) for item in value)
    if abs(sum(item * item for item in result) - 1.) > .001:
        raise ValueError("appearance descriptor must have unit norm")
    return result


def validate_appearances(values, count: int) -> list[tuple[float, ...] | None]:
    if not isinstance(values, list) or len(values) != count or count > MAX_DETECTIONS:
        raise ValueError("appearance descriptors must align with current detections")
    return [validate_descriptor(value) for value in values]


def similarity(left, right) -> float | None:
    """Compare already validated descriptors; unavailable evidence has no score."""
    if left is None or right is None:
        return None
    return min(1., max(0., sum(a * b for a, b in zip(left, right))))


class AppearanceEncoder:
    """Stateless image encoder with fixed-size output and no model/network I/O.

    Nearby background frequencies downweight crop background; this heuristic is
    not person segmentation. A similar descriptor does not prove human identity.
    The detector's process-wide OpenCV thread setting remains unchanged.
    """

    def __init__(self):
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("appearance requires the optional ARGOS camera dependency") from exc
        self._cv2, self._np = cv2, np

    def encode(self, jpeg: bytes, detections: list, *, width: int, height: int) -> list[tuple[float, ...] | None]:
        if (type(width) is not int or type(height) is not int
                or not 1 <= width <= 4096 or not 1 <= height <= 4096):
            raise ValueError("appearance image dimensions must be between 1 and 4096")
        if not isinstance(jpeg, bytes) or not jpeg or len(jpeg) > 16 * 1024 * 1024:
            raise ValueError("appearance requires a nonempty JPEG of at most 16 MiB")
        if not isinstance(detections, list) or len(detections) > MAX_DETECTIONS:
            raise ValueError("appearance accepts at most 16 current detections")
        boxes = []
        for detection in detections:
            box = detection.get("box") if isinstance(detection, dict) else None
            if (not isinstance(box, (list, tuple)) or len(box) != 4
                    or not all(_number(v) and 0 <= v <= 1 for v in box)
                    or box[2] <= 0 or box[3] <= 0
                    or box[0] + box[2] > 1 + 1e-9 or box[1] + box[3] > 1 + 1e-9):
                raise ValueError("appearance needs finite normalized detection boxes")
            boxes.append(tuple(float(v) for v in box))
        cv2, np = self._cv2, self._np
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.ndim != 3 or image.shape != (height, width, 3):
            raise ValueError("appearance JPEG dimensions differ from the detector result")
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        return [self._describe(rgb, box) for box in boxes]

    def _hsv(self, rgb):
        return self._cv2.cvtColor(rgb, self._cv2.COLOR_RGB2HSV_FULL).astype(self._np.float64) / 255.

    def _bins(self, values):
        np = self._np
        hue, saturation, value = values[..., 0], values[..., 1], values[..., 2]
        return (np.minimum(11, (hue * 12).astype(int)) * 32
                + np.minimum(3, (saturation * 4).astype(int)) * 8
                + np.minimum(7, (value * 8).astype(int)))

    def _describe(self, rgb, box):
        cv2, np = self._cv2, self._np
        height, width = rgb.shape[:2]
        x, y, w, h = box
        x0, y0 = math.floor(x * width), math.floor(y * height)
        x1, y1 = math.ceil((x + w) * width), math.ceil((y + h) * height)
        if (x0 < 0 or y0 < 0 or x1 > width or y1 > height
                or x1 - x0 < 12 or y1 - y0 < 32
                or min(x, y, 1 - x - w, 1 - y - h) < .005):
            return None
        # Histogram smoothing must not turn a spatially uniform crop into
        # apparent foreground evidence merely because the ring has fewer pixels.
        if float(np.max(np.std(rgb[y0:y1, x0:x1], axis=(0, 1)))) < 3.:
            return None
        ex0, ey0 = max(0, math.floor((x - .35 * w) * width)), max(0, math.floor((y - .1 * h) * height))
        ex1, ey1 = min(width, math.ceil((x + 1.35 * w) * width)), min(height, math.ceil((y + 1.1 * h) * height))
        ring_mask = np.ones((ey1 - ey0, ex1 - ex0), dtype=bool)
        ring_mask[y0 - ey0:y1 - ey0, x0 - ex0:x1 - ex0] = False
        ring_hsv = self._hsv(rgb[ey0:ey1, ex0:ex1])[ring_mask]
        patch = cv2.resize(rgb[y0:y1, x0:x1], (32, 96), interpolation=cv2.INTER_LINEAR)
        values = self._hsv(patch)
        codes = self._bins(values)
        foreground = np.bincount(codes.ravel(), minlength=384).astype(float) + 1.
        background = np.bincount(self._bins(ring_hsv).ravel(), minlength=384).astype(float) + 1.
        foreground /= foreground.sum()
        background /= background.sum()
        saliency = np.maximum(0., 1 - background[codes] / np.maximum(foreground[codes], 1e-9))
        horizontal = np.exp(-.5 * ((np.arange(32) + .5 - 16) / 9.) ** 2)
        weights = saliency * horizontal[None, :]
        band_mass = [float(band.mean()) for band in np.split(weights, 4)]
        if weights.mean() < .12 or sum(value >= .08 for value in band_mass) < 3:
            return None
        gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY).astype(float) / 255.
        gy, gx = np.gradient(gray)
        angle = np.mod(np.arctan2(gy, gx), np.pi)
        magnitude = np.hypot(gx, gy)
        blocks = []
        for rows, band_weight in zip(np.split(np.arange(96), 4), (.1, .35, .35, .2)):
            region, weight = values[rows], weights[rows]
            hue_sat = np.minimum(11, (region[..., 0] * 12).astype(int)) * 3 + np.minimum(2, (region[..., 1] * 3).astype(int))
            value = np.minimum(7, (region[..., 2] * 8).astype(int))
            orientation = np.minimum(7, (angle[rows] / np.pi * 8).astype(int))
            for code, evidence, count, block_weight in (
                    (hue_sat, weight, 36, .65), (value, weight, 8, .2),
                    (orientation, weight * magnitude[rows], 8, .15)):
                histogram = np.bincount(code.ravel(), weights=evidence.ravel(), minlength=count) + 1e-6
                histogram /= histogram.sum()
                blocks.append(np.sqrt(histogram * band_weight * block_weight))
        return validate_descriptor(tuple(float(value) for value in np.concatenate(blocks)))
