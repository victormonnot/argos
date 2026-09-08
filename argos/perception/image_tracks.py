"""Short-lived image-box associations for the passive person overlay.

IDs connect nearby detections of compatible size, not human identities. There is no
motion prediction, re-identification, depth estimate or simulator-pose input.
Only actual detections from the latest update are returned to the caller.
"""
from __future__ import annotations

import math
from numbers import Real


TRACK_TTL = .7
MIN_IOU = .25
MAX_TRACKS = 16
NEARBY_MAX_GAP = .35


def _number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _detection(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("image tracker needs detection objects")
    box, confidence = value.get("box"), value.get("confidence")
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(_number(item) and 0 <= item <= 1 for item in box)
            or box[2] <= 0 or box[3] <= 0
            or box[0] + box[2] > 1 + 1e-9 or box[1] + box[3] > 1 + 1e-9
            or not _number(confidence) or not 0 <= confidence <= 1):
        raise ValueError("image tracker needs finite normalized boxes and confidence")
    return {"box": [float(item) for item in box], "confidence": float(confidence)}


def _iou(a, b) -> float:
    overlap_w = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    overlap_h = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    overlap = overlap_w * overlap_h
    return overlap / (a[2] * a[3] + b[2] * b[3] - overlap)


def _nearby(a, b) -> bool:
    """A narrow walking person may move most of a box width between detections.

    Permit that small displacement only at compatible scale, bounded both by
    image size and person size. This is an association gate, not an extrapolated
    observation. Ambiguous candidate pairs are refused by the caller.
    """
    width_ratio, height_ratio = b[2] / a[2], b[3] / a[3]
    dx = abs((b[0] + b[2] / 2) - (a[0] + a[2] / 2))
    dy = abs((b[1] + b[3] / 2) - (a[1] + a[3] / 2))
    return (
        .5 <= width_ratio <= 2 and .75 <= height_ratio <= 4 / 3
        and dx <= min(.06, max(a[2], b[2]))
        and dy <= min(.04, .25 * max(a[3], b[3]))
    )


class ImageTracker:
    """IoU matching with a conservative fallback for narrow moving person boxes.

    ``captured_at`` is supplied by the frame owner. Its provenance is unchanged:
    a local camera-receipt timestamp must not be relabeled as sensor capture time.
    Old detections are remembered briefly for association, never displayed as if
    measured again. Invalid updates leave the previous associations untouched.
    After IoU matching, only mutually unique nearby pairs from the last .35 s
    may match without enough overlap. Crossings can still confuse image-only IDs.
    """

    def __init__(self):
        self._tracks: dict[int, tuple[dict, float]] = {}
        self._last_at: float | None = None
        self._next_id = 1

    def reset(self) -> None:
        self._tracks.clear()
        self._last_at = None

    def update(self, detections: list, captured_at: float) -> list[dict]:
        if not _number(captured_at) or captured_at < 0:
            raise ValueError("image tracker needs a finite nonnegative frame timestamp")
        if self._last_at is not None and captured_at <= self._last_at:
            raise ValueError("image tracker frame timestamps must strictly increase")
        if not isinstance(detections, list) or len(detections) > MAX_TRACKS:
            raise ValueError("image tracker accepts at most 16 detections per frame")
        current = [_detection(value) for value in detections]
        remembered = {
            identity: record for identity, record in self._tracks.items()
            if captured_at - record[1] <= TRACK_TTL
        }
        candidates = []
        for index, detection in enumerate(current):
            for identity, (previous, _) in remembered.items():
                overlap = _iou(detection["box"], previous["box"])
                if overlap >= MIN_IOU:
                    candidates.append((-overlap, identity, index))
        matches, assigned = {}, set()
        for _, identity, index in sorted(candidates):
            if index not in matches and identity not in assigned:
                matches[index] = identity
                assigned.add(identity)
        nearby_by_detection, nearby_by_track = {}, {}
        for index, detection in enumerate(current):
            if index in matches:
                continue
            for identity, (previous, seen_at) in remembered.items():
                if identity in assigned or captured_at - seen_at > NEARBY_MAX_GAP:
                    continue
                if _nearby(previous["box"], detection["box"]):
                    nearby_by_detection.setdefault(index, []).append(identity)
                    nearby_by_track.setdefault(identity, []).append(index)
        for index, identities in nearby_by_detection.items():
            if len(identities) == 1 and len(nearby_by_track[identities[0]]) == 1:
                matches[index] = identities[0]
        result = []
        for index, detection in enumerate(current):
            identity = matches.get(index)
            if identity is None:
                identity = self._next_id
                self._next_id += 1
            remembered[identity] = (detection, float(captured_at))
            # Do not share mutable boxes with the caller or returned observations.
            result.append({**detection, "box": list(detection["box"]), "track_id": identity})
        newest = sorted(remembered, key=lambda key: remembered[key][1], reverse=True)
        self._tracks = {identity: remembered[identity] for identity in newest[:MAX_TRACKS]}
        self._last_at = float(captured_at)
        return result
