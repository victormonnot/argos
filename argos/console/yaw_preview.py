"""Pure horizontal image-framing preview, with no command or transport path.

The output is a hypothetical normalized stick value, not an angular speed or a
radio command. Image receipt times use the caller's monotonic session clock;
neither polling nor a repeated image can make a target fresh again.
"""
from __future__ import annotations

import math

from argos.perception.appearance import similarity, validate_descriptor


FRAME_MAX_AGE = .45
YAW_LIMIT = .125
DEADBAND = .035
GAIN = .25
CONTINUOUS_YAW_LIMIT = .2
CONTINUOUS_GAIN = .5
MIN_CONFIDENCE = .5
RECOVERY_MAX_GAP = .7
CONTINUOUS_RECOVERY_MAX_GAP = 3.
CONTINUOUS_RECOVERY_MIN_SIMILARITY = .88
CONTINUOUS_RECOVERY_MAX_DX = .25
CONTINUOUS_RECOVERY_MAX_DY = .15
CENTER_SELECTION_MARGIN = .02
MAX_SAFE_INTEGER = 2**53 - 1


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _time(value):
    if not _number(value) or value < 0:
        raise ValueError("Preview requires a finite nonnegative session time")
    return float(value)


def _identity(value):
    return type(value) is int and 1 <= value <= MAX_SAFE_INTEGER


def _observation(value):
    """Validate and copy metadata; never retain caller-owned detection lists."""
    if not isinstance(value, dict):
        raise ValueError("No recent analyzed image")
    context = value["run_id"], value["video_id"]
    sequence = value["sequence"]
    width, height = value["width"], value["height"]
    if (any(not isinstance(item, str) or not item for item in context)
            or not _identity(sequence)
            or any(type(item) is not int or not 1 <= item <= 4096
                   for item in (width, height))):
        raise ValueError("Invalid analyzed image identity or dimensions")
    incoming = value["detections"]
    if not isinstance(incoming, list) or len(incoming) > 16:
        raise ValueError("Invalid analyzed image detections")
    detections, identities = [], set()
    for detection in incoming:
        if not isinstance(detection, dict):
            raise ValueError("Invalid person detection")
        identity = detection["track_id"]
        box, confidence = detection["box"], detection["confidence"]
        if (not _identity(identity) or identity in identities
                or not isinstance(box, (list, tuple)) or len(box) != 4
                or any(not _number(v) or not 0 <= v <= 1 for v in box)
                or box[2] <= 0 or box[3] <= 0
                or box[0] + box[2] > 1.000001
                or box[1] + box[3] > 1.000001
                or not _number(confidence) or not 0 <= confidence <= 1):
            raise ValueError("Invalid person bounds, confidence or identity")
        identities.add(identity)
        detections.append({"track_id": identity, "box": tuple(map(float, box)),
                           "confidence": float(confidence)})
    appearances = value.get("appearances")
    if appearances is not None:
        if not isinstance(appearances, (list, tuple)) or len(appearances) != len(detections):
            raise ValueError("Appearance metadata must align with detections")
        for detection, appearance in zip(detections, appearances):
            detection["appearance"] = validate_descriptor(appearance)
    return {"context": context, "dimensions": (width, height),
            "sequence": sequence, "received_at": _time(value["received_at"]),
            "detections": detections}


class YawPreview:
    """An explicitly selected image identity and bounded, latched preview.

    A short detection gap retains selection without producing a correction.
    The diagnostic default recovers only the same track. Opt-in continuous mode
    retains selection for three seconds and may associate a unique new track
    using two fresh images with compatible appearance and nearby geometry.
    Invalid or stale imagery and expired recovery still latch a cleared target.
    Entering a gap changes revision, fencing command sessions even if they miss
    the paused snapshot. Clear, expiry and source reset also change revision.
    Selection epoch is separate: a recoverable gap retains it, while an explicit
    selection or terminal loss changes it for continuous consumers. These short
    appearance associations are not proof of a person's real-world identity.
    """

    def __init__(self, enabled: bool, *, continuous=False, on_recovery=None):
        if on_recovery is not None and not callable(on_recovery):
            raise ValueError("Recovery diagnostics require a callable")
        self.enabled = bool(enabled)
        self.continuous = bool(continuous)
        self.yaw_limit = CONTINUOUS_YAW_LIMIT if self.continuous else YAW_LIMIT
        self.gain = CONTINUOUS_GAIN if self.continuous else GAIN
        self._on_recovery = on_recovery
        self._diagnostic_frame = None
        self._diagnostic_candidates = set()
        self.recovery_max_gap = (CONTINUOUS_RECOVERY_MAX_GAP if self.continuous
                                 else RECOVERY_MAX_GAP)
        self.phase = "idle" if self.enabled else "disabled"
        self.revision = 0
        self.selection_epoch = 0
        self._detail = ("Select a person in a recent analyzed image" if self.enabled
                        else "Yaw preview requires a real camera and person detection")
        self._frame = None
        self._last_frame = None
        self._last_now = None
        self._target_id = None
        self._selection_id = None
        self._last_seen_at = None
        self._reference = None
        self._reference_received_at = None
        self._recovery_candidate = None
        self._error_x = None
        self._yaw = 0.

    def _stop(self, detail):
        if self.phase in ("tracking", "paused"):
            self.phase = "stopped"
            self.revision += 1
            self.selection_epoch += 1
            self._detail = detail
        self._target_id = None
        self._selection_id = None
        self._last_seen_at = None
        self._reference = self._recovery_candidate = None
        self._reference_received_at = None
        self._error_x = None
        self._yaw = 0.

    def _clock(self, now):
        try:
            now = _time(now)
        except ValueError:
            self._stop("Preview clock is invalid; select the person again")
            self._frame = None
            raise
        if self._last_now is not None and now < self._last_now:
            self._stop("Preview clock moved backwards; select the person again")
            self._frame = None
            return now, False
        self._last_now = now
        return now, True

    def _target(self, now, track_id):
        if self._frame is None:
            raise RuntimeError("No recent analyzed image")
        if not 0 <= now - self._frame["received_at"] <= FRAME_MAX_AGE:
            raise RuntimeError("Selected target image is stale")
        target = next((item for item in self._frame["detections"]
                       if item["track_id"] == track_id), None)
        if target is None:
            raise RuntimeError("Selected person was lost; select a person again")
        if target["confidence"] < MIN_CONFIDENCE:
            raise RuntimeError("Selected person confidence is too low")
        return target

    def _update(self, now):
        if self.phase not in ("tracking", "paused"):
            return
        if self._frame is None:
            self._recovery_diagnostics(now, "image_unavailable")
            self._stop("No recent analyzed image")
            return
        if not 0 <= now - self._frame["received_at"] <= FRAME_MAX_AGE:
            self._recovery_diagnostics(now, "stale_image")
            self._stop("Selected target image is stale")
            return
        deadline = self._last_seen_at + self.recovery_max_gap
        if (self.phase == "paused" or self.continuous) and now >= deadline:
            self._recovery_diagnostics(now, "recovery_deadline")
            self._stop("Selected person was lost; select a person again")
            return
        target = next((item for item in self._frame["detections"]
                       if item["track_id"] == self._target_id), None)
        strong = [item for item in self._frame["detections"]
                  if item["confidence"] >= MIN_CONFIDENCE]
        lost = target is None or target["confidence"] < MIN_CONFIDENCE
        if self.continuous and (self.phase == "paused" or lost) and len(strong) > 1:
            self._recovery_diagnostics(now, "ambiguous_candidates")
            self._stop("Target recovery is ambiguous; cycle SC to select the centered person")
            return
        if lost:
            if now >= deadline:
                self._stop("Selected person was lost; select a person again")
                return
            if self.phase != "paused":
                self.revision += 1
            self.phase = "paused"
            self._detail = "Person briefly obscured or uncertain; preview paused with zero correction"
            self._error_x = None
            self._yaw = 0.
            target = self._recover_unique(strong, now) if self.continuous else None
            if target is None:
                return
            self._target_id = target["track_id"]
        if (self.phase == "paused"
                and self._frame["received_at"] <= self._last_seen_at):
            # A changed sequence with the old receipt cannot renew evidence.
            self._emit_recovery(target, now, False, "old_image_receipt")
            return
        if self.phase == "paused":
            self._emit_recovery(target, now, True, "accepted_same_track")
        self.phase = "tracking"
        self._detail = "Horizontal yaw preview only; no commands sent"
        self._last_seen_at = self._frame["received_at"]
        # A strong detection may lack a usable appearance crop. Keep the last
        # descriptor with its own box and receipt, never the newer crop's bounds
        # or polling time. Current tracking still uses the current target below.
        if (target.get("appearance") is not None or self._reference is None
                or self._reference["track_id"] != target["track_id"]
                or self._reference.get("appearance") is None):
            self._reference = target
            self._reference_received_at = (self._frame["received_at"]
                                           if target.get("appearance") is not None else None)
        self._recovery_candidate = None
        x, _, width, _ = target["box"]
        self._error_x = max(-1., min(1., 2 * (x + width / 2 - .5)))
        self._yaw = (0. if abs(self._error_x) <= DEADBAND else
                     max(-self.yaw_limit, min(self.yaw_limit, self.gain * self._error_x)))

    def _recovery_metrics(self, target, now):
        metrics = dict(similarity=None, dx=None, dy=None, width_ratio=None, height_ratio=None,
                       reference_appearance_available=(self._reference is not None
                                                       and self._reference.get("appearance") is not None),
                       candidate_appearance_available=(target is not None
                                                       and target.get("appearance") is not None),
                       reference_appearance_age_s=(None if self._reference_received_at is None else
                                                   now - self._reference_received_at))
        if target is None or self._reference is None:
            return metrics
        a, b = self._reference["box"], target["box"]
        metrics.update(similarity=similarity(self._reference.get("appearance"), target.get("appearance")),
                       dx=abs(b[0] + b[2] / 2 - a[0] - a[2] / 2),
                       dy=abs(b[1] + b[3] / 2 - a[1] - a[3] / 2),
                       width_ratio=b[2] / a[2], height_ratio=b[3] / a[3])
        return metrics

    def _emit_recovery(self, target, now, accepted, reason, metrics=None):
        """Scalar diagnostic callback only; repeated HTTP polls add no events."""
        if not self.continuous or self._on_recovery is None:
            return
        frame = self._frame
        context = None if frame is None else frame["context"]
        frame_key = (context, None if frame is None else frame["sequence"], self.selection_epoch)
        if frame_key != self._diagnostic_frame:
            self._diagnostic_frame = frame_key
            self._diagnostic_candidates = set()
        identity = None if target is None else target["track_id"]
        if identity in self._diagnostic_candidates:
            return
        self._diagnostic_candidates.add(identity)
        record = {
            "event": "yaw_recovery", "at": now,
            "run_id": None if context is None else context[0],
            "video_id": None if context is None else context[1],
            "frame_sequence": None if frame is None else frame["sequence"],
            "frame_received_at": None if frame is None else frame["received_at"],
            "selection_id": self._selection_id, "selection_epoch": self.selection_epoch,
            "previous_track_id": self._target_id, "track_id": identity,
            "confidence": None if target is None else target["confidence"],
            **(self._recovery_metrics(target, now) if metrics is None else metrics),
            "accepted": bool(accepted), "reason": reason,
        }
        try:
            self._on_recovery(record)
        except Exception:
            # Diagnostics must neither grant nor withdraw target authority.
            pass

    def _recovery_diagnostics(self, now, reason):
        candidates = [] if self._frame is None else self._frame["detections"]
        for target in candidates or [None]:
            self._emit_recovery(target, now, False, reason)

    def _recover_unique(self, strong, now):
        """Two real images plus appearance and geometry, never uniqueness alone."""
        for target in self._frame["detections"]:
            if target["confidence"] < MIN_CONFIDENCE:
                self._emit_recovery(target, now, False, "low_confidence")
        if len(strong) != 1 or self._reference is None:
            if not self._frame["detections"]:
                self._emit_recovery(None, now, False, "no_candidate")
            elif self._reference is None:
                self._recovery_diagnostics(now, "no_reference")
            self._recovery_candidate = None
            return None
        target = strong[0]
        metrics = self._recovery_metrics(target, now)
        reason = None
        if metrics["similarity"] is None:
            reason = "appearance_unavailable"
        elif not 0 <= metrics["reference_appearance_age_s"] < self.recovery_max_gap:
            reason = "reference_appearance_expired"
        elif metrics["similarity"] < CONTINUOUS_RECOVERY_MIN_SIMILARITY:
            reason = "appearance_similarity"
        elif not .5 <= metrics["width_ratio"] <= 2:
            reason = "width_ratio"
        elif not .75 <= metrics["height_ratio"] <= 4 / 3:
            reason = "height_ratio"
        elif metrics["dx"] > CONTINUOUS_RECOVERY_MAX_DX + 1e-12:
            reason = "horizontal_distance"
        elif metrics["dy"] > CONTINUOUS_RECOVERY_MAX_DY + 1e-12:
            reason = "vertical_distance"
        elif self._frame["received_at"] <= self._last_seen_at:
            reason = "old_image_receipt"
        if reason is not None:
            self._emit_recovery(target, now, False, reason, metrics)
            self._recovery_candidate = None
            return None
        candidate = (target["track_id"], self._frame["sequence"], self._frame["received_at"])
        previous = self._recovery_candidate
        if (previous is not None and previous[0] == candidate[0]
                and candidate[1] > previous[1] and candidate[2] > previous[2]):
            self._emit_recovery(target, now, True, "accepted_appearance", metrics)
            return target
        if previous is None or previous[0] != candidate[0]:
            self._recovery_candidate = candidate
        self._emit_recovery(target, now, False, "pending_second_image", metrics)
        return None

    def observe(self, observation, now):
        """Accept only server-owned image metadata, never renewing its receipt."""
        now, valid_clock = self._clock(now)
        if not valid_clock or not self.enabled:
            return
        try:
            candidate = _observation(observation)
            if candidate["received_at"] > now:
                raise ValueError("Image receipt is in the future")
        except (KeyError, TypeError, ValueError, OverflowError):
            self._frame = None
            if self.phase in ("tracking", "paused"):
                self._emit_recovery(None, now, False,
                                    "image_unavailable" if observation is None else "invalid_image")
            self._stop("No recent analyzed image" if observation is None else
                       "Invalid analyzed image metadata")
            return
        previous = self._last_frame
        if previous is not None:
            same_context = (candidate["context"] == previous["context"]
                            and candidate["dimensions"] == previous["dimensions"])
            if not same_context:
                revision = self.revision
                self._stop("Camera source or image dimensions changed; select the person again")
                if self.revision == revision:
                    # Also invalidate a pending selection made while idle.
                    self.revision += 1
                    self.selection_epoch += 1
            elif (candidate["sequence"] < previous["sequence"]
                  or candidate["received_at"] < previous["received_at"]
                  or (candidate["sequence"] == previous["sequence"]
                      and candidate != previous)):
                self._frame = None
                self._stop("Analyzed image order or identity changed; select the person again")
                return
        self._frame = self._last_frame = candidate
        # Decide using the newest completed image available at this instant.
        # An earlier state/observe expiry is already latched and cannot resume.
        self._update(now)

    def select(self, track_id, *, revision, now):
        """Select a currently present target using the current server revision."""
        now, valid_clock = self._clock(now)
        self._update(now)
        if not self.enabled:
            raise RuntimeError("Yaw preview is disabled")
        if not valid_clock:
            raise RuntimeError("Preview clock moved backwards")
        if type(revision) is not int or revision != self.revision:
            raise RuntimeError("Yaw preview changed; select the person again")
        if not _identity(track_id):
            raise ValueError("A positive safe-integer target identity is required")
        self._target(now, track_id)
        # An explicit selection starts its own appearance evidence, even when
        # selecting the same numeric track again after a crop became unusable.
        self._reference = self._recovery_candidate = None
        self._reference_received_at = None
        self._target_id = track_id
        self._selection_id = track_id
        self._last_seen_at = self._frame["received_at"]
        self.phase = "tracking"
        self._detail = "Horizontal yaw preview only; no commands sent"
        self.revision += 1
        self.selection_epoch += 1
        self._update(now)
        return self._snapshot(now)

    def select_center(self, *, now):
        """An explicit radio cycle retains a valid target or chooses the center.

        No request stays pending waiting for somebody to walk into the image.
        Near ties are refused so an explicit gesture cannot choose arbitrarily.
        """
        if not self.continuous:
            raise RuntimeError("Radio target selection requires continuous yaw assistance")
        now, valid_clock = self._clock(now)
        self._update(now)
        if not self.enabled or not valid_clock:
            raise RuntimeError("Yaw assistance is unavailable")
        if self.phase == "tracking":
            return self._snapshot(now)
        if self._frame is None or not 0 <= now - self._frame["received_at"] <= FRAME_MAX_AGE:
            raise RuntimeError("No recent analyzed image for radio selection")
        candidates = []
        for target in self._frame["detections"]:
            if target["confidence"] < MIN_CONFIDENCE:
                continue
            x, y, width, height = target["box"]
            candidates.append((math.hypot(x + width / 2 - .5, y + height / 2 - .5),
                               target["track_id"]))
        candidates.sort()
        if not candidates:
            raise RuntimeError("No confident person is visible; cycle SC again when ready")
        if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < CENTER_SELECTION_MARGIN:
            raise RuntimeError("Centered person is ambiguous; reframe and cycle SC again")
        return self.select(candidates[0][1], revision=self.revision, now=now)

    def clear(self, reason="Preview cleared"):
        """Cancel selection, including any select request from an older revision."""
        self._target_id = None
        self._selection_id = None
        self._last_seen_at = None
        self._reference = self._recovery_candidate = None
        self._reference_received_at = None
        self._error_x = None
        self._yaw = 0.
        self.revision += 1
        self.selection_epoch += 1
        self.phase = "idle" if self.enabled else "disabled"
        self._detail = str(reason)

    def _snapshot(self, now):
        frame = self._frame
        context = None if frame is None else frame["context"]
        return {
            "enabled": self.enabled, "phase": self.phase, "detail": self._detail,
            "revision": self.revision, "target_id": self._target_id,
            "selection_epoch": self.selection_epoch,
            "selection_id": self._selection_id, "continuous": self.continuous,
            "run_id": None if context is None else context[0],
            "video_id": None if context is None else context[1],
            "frame_sequence": None if frame is None else frame["sequence"],
            "frame_received_at": None if frame is None else frame["received_at"],
            "frame_age_s": None if frame is None else max(0., now - frame["received_at"]),
            "frame_max_age_s": FRAME_MAX_AGE, "error_x": self._error_x,
            "yaw": self._yaw, "yaw_limit": self.yaw_limit, "deadband": DEADBAND,
            "recovery_max_gap_s": self.recovery_max_gap,
            "recovery_deadline_at": (self._last_seen_at + self.recovery_max_gap
                                     if self.phase == "paused" else None),
        }

    def state(self, now):
        """Read a snapshot, expiring stale selection without renewing any image."""
        now, _ = self._clock(now)
        self._update(now)
        return self._snapshot(now)
