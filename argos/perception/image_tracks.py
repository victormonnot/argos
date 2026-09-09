"""Short-lived associations between actual image detections, not human identity.

Ordinary geometric matches must be mutually unique. Optional appearance evidence
can veto contradictions and support a bounded additional motion match. No pixels,
motion predictions, simulator poses or flight commands enter this class.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real

from .appearance import (GROSS_CONTRADICTION, MAX_APPEARANCE_AGE, MIN_MARGIN,
                         MIN_SIMILARITY, similarity, validate_appearances)


TRACK_TTL = .7
MIN_IOU = .25
MAX_TRACKS = 16
NEARBY_MAX_GAP = .35
STRONG_CONFIDENCE = .5


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


def _expanded_geometry(a, b) -> bool:
    width_ratio, height_ratio = b[2] / a[2], b[3] / a[3]
    dx = abs(b[0] + b[2] / 2 - a[0] - a[2] / 2)
    dy = abs(b[1] + b[3] / 2 - a[1] - a[3] / 2)
    return (.5 <= width_ratio <= 2 and .75 <= height_ratio <= 4 / 3
            and dx <= min(.13, 3 * max(a[2], b[2]))
            and dy <= min(.05, .35 * max(a[3], b[3])))


@dataclass(frozen=True)
class _Track:
    # Presence in memory requires a past strong detection. A later weak sample
    # can update seen_at/geometry but never the strong appearance reference.
    detection: dict
    seen_at: float
    appearance: tuple[float, ...] | None
    appearance_at: float | None

    def recent_appearance(self, now):
        if self.appearance_at is not None and 0 <= now - self.appearance_at <= MAX_APPEARANCE_AGE:
            return self.appearance
        return None


class ImageTracker:
    """Association owner; source/run/dimension fencing remains with its caller.

    ``captured_at`` retains its existing local camera-receipt provenance. Only
    current detections are returned. Strong established tracks compete first;
    new weak boxes get display IDs without persistent association memory.
    Empty frames do not refresh detection or appearance timestamps. Crossing or
    replacement people can still confuse these short-lived image IDs.
    """

    def __init__(self):
        self._tracks: dict[int, _Track] = {}
        self._last_at: float | None = None
        self._next_id = 1
        self._appearance_mode = False

    def reset(self) -> None:
        self._tracks.clear()
        self._last_at = None
        self._appearance_mode = False

    @staticmethod
    def _geometry(current, descriptors, records, indices, identities, now):
        by_detection, by_track = {}, {}
        for index in indices:
            for identity in identities:
                record = records[identity]
                previous, box = record.detection["box"], current[index]["box"]
                if not (_iou(previous, box) >= MIN_IOU or (
                        now - record.seen_at <= NEARBY_MAX_GAP and _nearby(previous, box))):
                    continue
                score = similarity(record.recent_appearance(now), descriptors[index])
                if score is not None and score < GROSS_CONTRADICTION:
                    continue
                by_detection.setdefault(index, []).append(identity)
                by_track.setdefault(identity, []).append(index)
        matches = {
            index: candidates[0] for index, candidates in by_detection.items()
            if len(candidates) == 1 and len(by_track[candidates[0]]) == 1
        }
        blocked_indices = {index for index, candidates in by_detection.items()
                           if len(candidates) > 1 or any(len(by_track[i]) > 1 for i in candidates)}
        blocked_ids = {identity for identity, candidates in by_track.items()
                       if len(candidates) > 1 or any(len(by_detection[i]) > 1 for i in candidates)}
        return matches, blocked_indices, blocked_ids

    @staticmethod
    def _appearance(current, descriptors, records, indices, identities, now, previous_at):
        scores = {}
        for index in indices:
            for identity in identities:
                record = records[identity]
                if (record.seen_at != previous_at
                        or now - record.seen_at > MAX_APPEARANCE_AGE
                        or not _expanded_geometry(record.detection["box"], current[index]["box"])):
                    continue
                score = similarity(record.recent_appearance(now), descriptors[index])
                if score is not None:
                    scores[index, identity] = score
        matches = {}
        for (index, identity), score in scores.items():
            if score < MIN_SIMILARITY:
                continue
            alternatives = [value for (other_index, other_id), value in scores.items()
                            if (other_index == index or other_id == identity)
                            and (other_index, other_id) != (index, identity)]
            if score - max(alternatives, default=0.) >= MIN_MARGIN:
                matches[index] = identity
        return matches

    def update(self, detections: list, captured_at: float, *, appearances=None) -> list[dict]:
        if not _number(captured_at) or captured_at < 0:
            raise ValueError("image tracker needs a finite nonnegative frame timestamp")
        if self._last_at is not None and captured_at <= self._last_at:
            raise ValueError("image tracker frame timestamps must strictly increase")
        if not isinstance(detections, list) or len(detections) > MAX_TRACKS:
            raise ValueError("image tracker accepts at most 16 detections per frame")
        current = [_detection(value) for value in detections]
        if appearances is None:
            if self._appearance_mode:
                raise ValueError("appearance metadata cannot disappear within a source")
            descriptors = [None] * len(current)
        else:
            descriptors = validate_appearances(appearances, len(current))
        remembered = {identity: record for identity, record in self._tracks.items()
                      if captured_at - record.seen_at <= TRACK_TTL}
        strong = [i for i, detection in enumerate(current) if detection["confidence"] >= STRONG_CONFIDENCE]
        weak = [i for i, detection in enumerate(current) if detection["confidence"] < STRONG_CONFIDENCE]
        matches, blocked_indices, blocked_ids = self._geometry(
            current, descriptors, remembered, strong, list(remembered), captured_at)
        expanded = self._appearance(current, descriptors, remembered,
            [i for i in strong if i not in matches and i not in blocked_indices],
            [identity for identity in remembered if identity not in set(matches.values()) | blocked_ids], captured_at, self._last_at)
        matches.update(expanded)
        weak_matches, _, _ = self._geometry(current, descriptors, remembered, weak,
            [identity for identity in remembered if identity not in set(matches.values()) | blocked_ids], captured_at)
        matches.update(weak_matches)
        result, next_id = [], self._next_id
        for index, detection in enumerate(current):
            identity = matches.get(index)
            previous = remembered.get(identity)
            if identity is None:
                identity, next_id = next_id, next_id + 1
            if detection["confidence"] >= STRONG_CONFIDENCE or previous is not None:
                appearance = previous.appearance if previous else None
                appearance_at = previous.appearance_at if previous else None
                if detection["confidence"] >= STRONG_CONFIDENCE and descriptors[index] is not None:
                    appearance, appearance_at = descriptors[index], float(captured_at)
                remembered[identity] = _Track(detection, float(captured_at), appearance, appearance_at)
            result.append({**detection, "box": list(detection["box"]), "track_id": identity})
        newest = sorted(remembered, key=lambda key: remembered[key].seen_at, reverse=True)
        self._tracks = {identity: remembered[identity] for identity in newest[:MAX_TRACKS]}
        self._last_at, self._next_id = float(captured_at), next_id
        self._appearance_mode = self._appearance_mode or appearances is not None
        return result
