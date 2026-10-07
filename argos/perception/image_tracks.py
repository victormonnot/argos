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
    # False once this ID has appeared alongside another detected box, so a
    # genuine multi-person history never becomes an obsolete singleton later.
    solitary: bool

    def recent_appearance(self, now):
        if self.appearance_at is not None and 0 <= now - self.appearance_at <= MAX_APPEARANCE_AGE:
            return self.appearance
        return None


class ImageTracker:
    """Association owner; source/run/dimension fencing remains with its caller.

    ``captured_at`` retains its existing local camera-receipt provenance. Only
    current detections are returned. Strong established tracks compete first;
    new weak boxes get display IDs without persistent association memory.
    Ambiguous strong geometry retires the implicated predecessor identities;
    later unambiguous observations can establish fresh associations.
    Empty frames do not refresh detection or appearance timestamps. Crossing or
    replacement people can still confuse these short-lived image IDs.

    ``on_diagnostic`` optionally receives one detached decision record after an
    accepted update. It contains box/scalar evidence, never pixels or appearance
    vectors. Diagnostics cannot alter associations through the supplied record;
    callback failures are ignored. The default path builds no diagnostic records.
    """

    def __init__(self, *, on_diagnostic=None):
        if on_diagnostic is not None and not callable(on_diagnostic):
            raise ValueError("Image association diagnostics require a callable")
        self._on_diagnostic = on_diagnostic
        self._tracks: dict[int, _Track] = {}
        self._last_at: float | None = None
        self._last_singleton = False
        self._next_id = 1
        self._appearance_mode = False

    def reset(self) -> None:
        self._tracks.clear()
        self._last_at = None
        self._last_singleton = False
        self._appearance_mode = False

    @staticmethod
    def _obsolete_singletons(current, descriptors, records, now, previous_at,
                             previous_singleton):
        """Expired singleton histories cannot outvote fresh sole continuity.

        A contradicted old ID can remain in geometric memory after a replacement
        is established. Once its appearance expires, that formerly rejected ID
        would become a competitor again merely because its veto disappeared.
        Retire it only when the immediately preceding sole strong observation
        still has compatible appearance and geometry. Multi-person histories,
        missing evidence and gaps keep the ordinary ambiguity rules.
        """
        if (not previous_singleton or len(current) != 1
                or current[0]["confidence"] < STRONG_CONFIDENCE):
            return set()
        predecessors = [(identity, record) for identity, record in records.items()
                        if record.seen_at == previous_at
                        and record.detection["confidence"] >= STRONG_CONFIDENCE]
        if len(predecessors) != 1:
            return set()
        identity, record = predecessors[0]
        box = current[0]["box"]
        previous = record.detection["box"]
        score = similarity(record.recent_appearance(now), descriptors[0])
        if (score is None or score < GROSS_CONTRADICTION
                or not (_iou(previous, box) >= MIN_IOU or (
                    now - record.seen_at <= NEARBY_MAX_GAP and _nearby(previous, box)))):
            return set()
        return {old_id for old_id, old in records.items()
                if old_id != identity and old.solitary and old.seen_at < previous_at
                and old.appearance_at is not None
                and now - old.appearance_at > MAX_APPEARANCE_AGE
                and (_iou(old.detection["box"], box) >= MIN_IOU or (
                    now - old.seen_at <= NEARBY_MAX_GAP
                    and _nearby(old.detection["box"], box)))}

    @staticmethod
    def _geometry(current, descriptors, records, indices, identities, now,
                  diagnostic=None, stage="geometry"):
        by_detection, by_track = {}, {}
        edges = [] if diagnostic is not None else None
        for index in indices:
            for identity in identities:
                record = records[identity]
                previous, box = record.detection["box"], current[index]["box"]
                edge = None
                if edges is not None:
                    edge = ImageTracker._pair_diagnostic(
                        index, identity, record, box, descriptors[index], now, stage)
                    edges.append(edge)
                if not (_iou(previous, box) >= MIN_IOU or (
                        now - record.seen_at <= NEARBY_MAX_GAP and _nearby(previous, box))):
                    if edge is not None:
                        edge["reason"] = "geometry_gate"
                    continue
                score = similarity(record.recent_appearance(now), descriptors[index])
                if score is not None and score < GROSS_CONTRADICTION:
                    if edge is not None:
                        edge["reason"] = "appearance_contradiction"
                    continue
                if edge is not None:
                    edge["reason"] = "candidate"
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
        if edges is not None:
            for edge in edges:
                if edge["reason"] == "candidate":
                    edge["accepted"] = matches.get(edge["detection_index"]) == edge["track_id"]
                    edge["reason"] = "matched" if edge["accepted"] else "ambiguous_geometry"
            diagnostic["edges"].extend(edges)
        return matches, blocked_indices, blocked_ids

    @staticmethod
    def _appearance(current, descriptors, records, indices, identities, now, previous_at,
                    diagnostic=None):
        scores = {}
        edges = {} if diagnostic is not None else None
        for index in indices:
            for identity in identities:
                record = records[identity]
                edge = None
                if edges is not None:
                    edge = ImageTracker._pair_diagnostic(
                        index, identity, record, current[index]["box"], descriptors[index],
                        now, "appearance_extension")
                    edges[index, identity] = edge
                if (record.seen_at != previous_at
                        or now - record.seen_at > MAX_APPEARANCE_AGE
                        or not _expanded_geometry(record.detection["box"], current[index]["box"])):
                    if edge is not None:
                        edge["reason"] = (
                            "not_previous_frame" if record.seen_at != previous_at else
                            "appearance_gap" if now - record.seen_at > MAX_APPEARANCE_AGE else
                            "expanded_geometry_gate")
                    continue
                score = similarity(record.recent_appearance(now), descriptors[index])
                if score is not None:
                    scores[index, identity] = score
                elif edge is not None:
                    edge["reason"] = "appearance_unavailable"
        matches = {}
        for (index, identity), score in scores.items():
            if score < MIN_SIMILARITY:
                if edges is not None:
                    edges[index, identity]["reason"] = "appearance_similarity"
                continue
            alternatives = [value for (other_index, other_id), value in scores.items()
                            if (other_index == index or other_id == identity)
                            and (other_index, other_id) != (index, identity)]
            if score - max(alternatives, default=0.) >= MIN_MARGIN:
                matches[index] = identity
            if edges is not None:
                edge = edges[index, identity]
                edge["margin"] = score - max(alternatives, default=0.)
                edge["accepted"] = matches.get(index) == identity
                edge["reason"] = "matched" if edge["accepted"] else "appearance_margin"
        if edges is not None:
            diagnostic["edges"].extend(edges.values())
        return matches

    @staticmethod
    def _pair_diagnostic(index, identity, record, box, descriptor, now, stage):
        previous = record.detection["box"]
        return {
            "stage": stage, "detection_index": index, "track_id": identity,
            "age_s": now - record.seen_at,
            "iou": _iou(previous, box),
            "dx": abs(box[0] + box[2] / 2 - previous[0] - previous[2] / 2),
            "dy": abs(box[1] + box[3] / 2 - previous[1] - previous[3] / 2),
            "width_ratio": box[2] / previous[2], "height_ratio": box[3] / previous[3],
            "nearby_geometry": _nearby(previous, box),
            "nearby_time": now - record.seen_at <= NEARBY_MAX_GAP,
            "expanded_geometry": _expanded_geometry(previous, box),
            "similarity": similarity(record.recent_appearance(now), descriptor),
            "accepted": False,
        }

    def _diagnostic(self, current, descriptors, now):
        return {
            "event": "image_association", "captured_at": float(now),
            "previous_at": self._last_at,
            "thresholds": {"track_ttl_s": TRACK_TTL, "min_iou": MIN_IOU,
                "nearby_max_gap_s": NEARBY_MAX_GAP, "strong_confidence": STRONG_CONFIDENCE,
                "max_appearance_age_s": MAX_APPEARANCE_AGE,
                "gross_contradiction": GROSS_CONTRADICTION,
                "min_similarity": MIN_SIMILARITY, "min_margin": MIN_MARGIN},
            "detections": [dict(detection_index=index, box=list(value["box"]),
                confidence=value["confidence"], strong=value["confidence"] >= STRONG_CONFIDENCE,
                appearance_available=descriptors[index] is not None)
                for index, value in enumerate(current)],
            "previous_tracks": [dict(track_id=identity, box=list(record.detection["box"]),
                confidence=record.detection["confidence"], seen_at=record.seen_at,
                age_s=now - record.seen_at, solitary=record.solitary,
                appearance_at=record.appearance_at,
                appearance_age_s=None if record.appearance_at is None else now - record.appearance_at,
                appearance_available=record.appearance is not None,
                appearance_fresh=record.recent_appearance(now) is not None)
                for identity, record in self._tracks.items()],
            "expired_track_ids": [identity for identity, record in self._tracks.items()
                                  if now - record.seen_at > TRACK_TTL],
            "retired_tracks": [], "edges": [], "associations": [],
        }

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
        diagnostic = (self._diagnostic(current, descriptors, captured_at)
                      if self._on_diagnostic is not None else None)
        remembered = {identity: record for identity, record in self._tracks.items()
                      if captured_at - record.seen_at <= TRACK_TTL}
        for identity in self._obsolete_singletons(
                current, descriptors, remembered, captured_at, self._last_at,
                self._last_singleton):
            del remembered[identity]
            if diagnostic is not None:
                diagnostic["retired_tracks"].append(
                    {"track_id": identity, "reason": "obsolete_singleton"})
        strong = [i for i, detection in enumerate(current) if detection["confidence"] >= STRONG_CONFIDENCE]
        weak = [i for i, detection in enumerate(current) if detection["confidence"] < STRONG_CONFIDENCE]
        matches, blocked_indices, blocked_ids = self._geometry(
            current, descriptors, remembered, strong, list(remembered), captured_at,
            diagnostic, "strong_geometry")
        reasons = {index: "strong_geometry" for index in matches} if diagnostic is not None else None
        # Predecessor identities are untrustworthy after strong ambiguity.
        # Keeping them alongside newly seeded tracks would make each new image
        # ambiguous again, even once a single person becomes stationary. Retire
        # only the implicated identities; do not guess between them or let an
        # appearance fallback / weak box revive them later in this update.
        for identity in blocked_ids:
            del remembered[identity]
            if diagnostic is not None:
                diagnostic["retired_tracks"].append(
                    {"track_id": identity, "reason": "strong_ambiguity"})
        expanded = self._appearance(current, descriptors, remembered,
            [i for i in strong if i not in matches and i not in blocked_indices],
            [identity for identity in remembered if identity not in set(matches.values()) | blocked_ids],
            captured_at, self._last_at, diagnostic)
        matches.update(expanded)
        weak_matches, _, _ = self._geometry(current, descriptors, remembered, weak,
            [identity for identity in remembered if identity not in set(matches.values()) | blocked_ids],
            captured_at, diagnostic, "weak_geometry")
        matches.update(weak_matches)
        if reasons is not None:
            reasons.update({index: "appearance_extension" for index in expanded})
            reasons.update({index: "weak_geometry" for index in weak_matches})
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
                solitary = len(current) == 1 and (previous is None or previous.solitary)
                remembered[identity] = _Track(detection, float(captured_at), appearance,
                                               appearance_at, solitary)
            result.append({**detection, "box": list(detection["box"]), "track_id": identity})
            if diagnostic is not None:
                diagnostic["associations"].append({
                    "detection_index": index, "track_id": identity,
                    "previous_track_id": matches.get(index),
                    "reason": reasons.get(index, "new_strong" if detection["confidence"] >=
                                          STRONG_CONFIDENCE else "weak_display_only"),
                    "blocked_by_strong_ambiguity": index in blocked_indices,
                    "remembered": identity in remembered,
                })
        newest = sorted(remembered, key=lambda key: remembered[key].seen_at, reverse=True)
        self._tracks = {identity: remembered[identity] for identity in newest[:MAX_TRACKS]}
        self._last_at, self._next_id = float(captured_at), next_id
        self._last_singleton = len(current) == 1
        self._appearance_mode = self._appearance_mode or appearances is not None
        if diagnostic is not None:
            diagnostic["retired_tracks"].extend({"track_id": identity, "reason": "memory_limit"}
                                                for identity in newest[MAX_TRACKS:])
            diagnostic["remembered_track_ids"] = list(self._tracks)
            try:
                self._on_diagnostic(diagnostic)
            except Exception:
                # Observation failures must not change tracking or its output.
                pass
        return result
