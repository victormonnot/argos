"""Explicit offline adapters; no production tracker selection is changed here.

The native association implementations may predict internally, but this boundary
returns only original detector measurements with exact source-row provenance.
Omitted detector rows remain inspectable through ``last_unassigned``. A caller
must keep them in any common selection/ambiguity policy; suppressing them would
compare different detector contracts.
"""
from __future__ import annotations

from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
import math
from threading import RLock
from types import SimpleNamespace

from .appearance import validate_appearances
from .image_tracks import ImageTracker, MAX_TRACKS, _detection


TRACKER_NAMES = ("image", "bytetrack", "botsort")
_NATIVE_LOCK = RLock()


def tracker_metadata(name):
    """Frozen settings, including native differences rather than implied parity."""
    if name not in TRACKER_NAMES:
        raise ValueError(f"unknown offline tracker: {name}")
    common = dict(name=name, detector_floor=.35, high_confidence=.5,
                  observation_boxes="original_detector_measurements",
                  learned_reid=False, unassigned_policy="reported_to_caller")
    if name == "image":
        return {**common, "implementation": "argos.perception.image_tracks.ImageTracker",
                "appearance": "existing_color_gradient_heuristic",
                "high_boundary": ">=", "birth_confidence": .5,
                "memory_seconds": .7, "age_unit": "camera_receipt_seconds"}
    deps = {}
    for package in ("numpy", "scipy", "lap", "cython_bbox", "opencv-python-headless"):
        try:
            deps[package] = version(package)
        except PackageNotFoundError:
            deps[package] = None
    return {**common, "implementation": "vendored_official",
            "repository": ("https://github.com/FoundationVision/ByteTrack" if name == "bytetrack"
                           else "https://github.com/NirAharon/BoT-SORT"),
            "commit": ("d1bf0191adff59bc8fcfeaa0b33d3d1642552a99" if name == "bytetrack"
                       else "251985436d6712aaf682aaaf5f71edb4987224bd"),
            "appearance": None, "high_boundary": ">",
            "exact_high_threshold_disposition": "unassigned_native_strict_boundary",
            "native_low_confidence": .1, "effective_low_confidence": .35,
            "birth_confidence": .6, "birth_boundary": ">=",
            "match_threshold": .8, "second_match_threshold": .5,
            "unconfirmed_match_threshold": .7, "score_fusion": True,
            "reference_frame_rate": 10, "track_buffer_argument": 21,
            "buffer_updates": 7, "age_unit": "analyzed_updates",
            "buffer_caveat": "7 updates is nominally .7s at 10Hz, not a wall-clock TTL",
            "unconfirmed_output": name == "botsort",
            "gmc": "sparseOptFlow" if name == "botsort" else None,
            "gmc_downscale": 2 if name == "botsort" else None,
            "dependencies": deps}


class _ImageAdapter:
    def __init__(self, on_diagnostic=None):
        self.metadata = tracker_metadata("image")
        self._tracker = ImageTracker(on_diagnostic=on_diagnostic)
        self.last_detection_indices = []
        self.last_output_confirmed = []
        self.last_unassigned = []

    def reset(self):
        self._tracker.reset()
        self.last_detection_indices = []
        self.last_output_confirmed = []
        self.last_unassigned = []

    def update(self, detections, captured_at, *, appearances=None, image=None,
               width=None, height=None):
        result = self._tracker.update(detections, captured_at, appearances=appearances)
        self.last_detection_indices = list(range(len(result)))
        self.last_output_confirmed = [row["track_id"] in self._tracker._tracks for row in result]
        self.last_unassigned = []
        return result


class _UpstreamAdapter:
    def __init__(self, name, on_diagnostic=None):
        self.name = name
        self.metadata = tracker_metadata(name)
        self._on_diagnostic = on_diagnostic
        self.reset()

    def reset(self):
        try:
            if self.name == "bytetrack":
                from ._vendor.bytetrack.byte_tracker import BYTETracker
                args = SimpleNamespace(track_thresh=.5, track_buffer=21,
                                       match_thresh=.8, mot20=False)
                self._native = BYTETracker(args, frame_rate=10)
                from ._vendor.bytetrack.basetrack import BaseTrack
            else:
                from ._vendor.botsort.bot_sort import BoTSORT
                args = SimpleNamespace(track_high_thresh=.5, track_low_thresh=.1,
                    new_track_thresh=.6, track_buffer=21, match_thresh=.8, mot20=False,
                    proximity_thresh=.5, appearance_thresh=.25, with_reid=False,
                    cmc_method="sparseOptFlow", name="argos-offline", ablation=False)
                with _NATIVE_LOCK:
                    self._native = BoTSORT(args, frame_rate=10)
                from ._vendor.botsort.basetrack import BaseTrack
        except ImportError as exc:
            raise ImportError("offline trackers require the tracking extra in an isolated environment") from exc
        self._base_track = BaseTrack
        self._id_count = 0
        self._last_at = None
        self._dimensions = None
        self.last_detection_indices = []
        self.last_output_confirmed = []
        self.last_unassigned = []

    def update(self, detections, captured_at, *, appearances=None, image=None,
               width=None, height=None):
        import numpy as np

        if (type(captured_at) not in (int, float) or not math.isfinite(captured_at)
                or captured_at < 0 or self._last_at is not None and captured_at <= self._last_at):
            raise ValueError("offline tracker timestamps must be finite, nonnegative and strictly increasing")
        if not isinstance(detections, list) or len(detections) > MAX_TRACKS:
            raise ValueError("offline tracker accepts at most 16 detections per frame")
        current = [_detection(row) for row in detections]
        if any(row["confidence"] < .35 for row in current):
            raise ValueError("frozen comparison detector floor is 0.35")
        if appearances is not None:
            validate_appearances(appearances, len(current))
        if type(width) is not int or type(height) is not int or width < 1 or height < 1:
            raise ValueError("native trackers require positive integer image dimensions")
        if self._dimensions is not None and self._dimensions != (width, height):
            raise ValueError("image dimensions changed; reset the offline tracker")
        if self.name == "botsort":
            if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                    or image.shape != (height, width, 3)):
                raise ValueError("BoT-SORT needs the original uint8 BGR frame at declared dimensions")
        measurements = np.empty((len(current), 5), dtype=np.float64)
        for index, row in enumerate(current):
            x, y, w, h = row["box"]
            measurements[index] = x * width, y * height, (x + w) * width, (y + h) * height, row["confidence"]
        before = {row.track_id: row.state for row in
                  self._native.tracked_stracks + self._native.lost_stracks}
        # The official algorithms use a module-wide ID counter. Isolating that
        # counter permits independent replay instances without changing assignment.
        with _NATIVE_LOCK:
            self._base_track._count = self._id_count
            if self.name == "bytetrack":
                native = self._native.update(measurements, (height, width), (height, width))
            else:
                native = self._native.update(measurements, image)
            self._id_count = self._base_track._count
        self._last_at, self._dimensions = float(captured_at), (width, height)
        result, indices, confirmed, associations = [], [], [], []
        for track in native:
            # A Kalman prediction must never acquire the freshness of this input.
            if track.frame_id != self._native.frame_id:
                continue
            index = track.detection_index
            if type(index) is not int or not 0 <= index < len(current) or index in indices:
                raise RuntimeError("native observation provenance is missing or not one-to-one")
            indices.append(index)
            confirmed.append(bool(track.is_activated))
            result.append({**deepcopy(current[index]), "track_id": int(track.track_id)})
            associations.append(dict(detection_index=index, track_id=int(track.track_id),
                previous_track_id=int(track.track_id) if track.track_id in before else None,
                reason="native_observed_association" if track.track_id in before else "native_birth",
                confirmed=bool(track.is_activated), remembered=True))
        unassigned = []
        pending = {row.detection_index for row in self._native.tracked_stracks
                   if row.frame_id == self._native.frame_id and not row.is_activated}
        for index, row in enumerate(current):
            if index in indices:
                continue
            score = row["confidence"]
            if index in pending:
                reason = "native_unconfirmed"
            elif score == .5:
                reason = "native_strict_high_boundary"
            elif score < .5:
                reason = "native_unmatched_low_confidence"
            elif score < .6:
                reason = "native_unmatched_below_birth_threshold"
            else:
                reason = "native_not_returned_or_duplicate_suppression"
            unassigned.append({**deepcopy(row), "detection_index": index, "reason": reason})
        self.last_detection_indices = indices
        self.last_output_confirmed = confirmed
        self.last_unassigned = unassigned
        if self._on_diagnostic is not None:
            record = dict(captured_at=float(captured_at), tracker=self.name,
                frame_id=self._native.frame_id, associations=associations,
                unassigned=deepcopy(unassigned),
                remembered_track_ids=[int(row.track_id) for row in self._native.tracked_stracks],
                lost_track_ids=[int(row.track_id) for row in self._native.lost_stracks],
                predictions_returned=False)
            try:
                self._on_diagnostic(record)
            except Exception:
                pass  # Instrumentation does not change association output.
        return result


def make_tracker(name, *, on_diagnostic=None):
    """Create a fresh explicitly chosen tracker, importing extras only on demand."""
    if name not in TRACKER_NAMES:
        raise ValueError(f"unknown offline tracker: {name}")
    return _ImageAdapter(on_diagnostic) if name == "image" else _UpstreamAdapter(name, on_diagnostic)
