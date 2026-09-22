"""Pure horizontal image-framing preview, with no command or transport path.

The output is a hypothetical normalized stick value, not an angular speed or a
radio command. Image receipt times use the caller's monotonic session clock;
neither polling nor a repeated image can make a target fresh again.
"""
from __future__ import annotations

import math


FRAME_MAX_AGE = .45
YAW_LIMIT = .125
DEADBAND = .035
GAIN = .25
MIN_CONFIDENCE = .5
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
    return {"context": context, "dimensions": (width, height),
            "sequence": sequence, "received_at": _time(value["received_at"]),
            "detections": detections}


class YawPreview:
    """An explicitly selected image identity and bounded, latched preview.

    Any loss of the selected detection or valid fresh imagery clears the target.
    A later valid image can be selected explicitly, but cannot resume tracking.
    Revisions invalidate requests created before clear, expiry or source reset.
    """

    def __init__(self, enabled: bool):
        self.enabled = bool(enabled)
        self.phase = "idle" if self.enabled else "disabled"
        self.revision = 0
        self._detail = ("Select a person in a recent analyzed image" if self.enabled
                        else "Yaw preview requires a real camera and person detection")
        self._frame = None
        self._last_frame = None
        self._last_now = None
        self._target_id = None
        self._error_x = None
        self._yaw = 0.

    def _stop(self, detail):
        if self.phase == "tracking":
            self.phase = "stopped"
            self.revision += 1
            self._detail = detail
        self._target_id = None
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
        if self.phase != "tracking":
            return
        try:
            target = self._target(now, self._target_id)
        except RuntimeError as exc:
            self._stop(str(exc))
            return
        x, _, width, _ = target["box"]
        self._error_x = max(-1., min(1., 2 * (x + width / 2 - .5)))
        self._yaw = (0. if abs(self._error_x) <= DEADBAND else
                     max(-YAW_LIMIT, min(YAW_LIMIT, GAIN * self._error_x)))

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
        self._target_id = track_id
        self.phase = "tracking"
        self._detail = "Horizontal yaw preview only; no commands sent"
        self.revision += 1
        self._update(now)
        return self._snapshot(now)

    def clear(self, reason="Preview cleared"):
        """Cancel selection, including any select request from an older revision."""
        self._target_id = None
        self._error_x = None
        self._yaw = 0.
        self.revision += 1
        self.phase = "idle" if self.enabled else "disabled"
        self._detail = str(reason)

    def _snapshot(self, now):
        frame = self._frame
        context = None if frame is None else frame["context"]
        return {
            "enabled": self.enabled, "phase": self.phase, "detail": self._detail,
            "revision": self.revision, "target_id": self._target_id,
            "run_id": None if context is None else context[0],
            "video_id": None if context is None else context[1],
            "frame_sequence": None if frame is None else frame["sequence"],
            "frame_received_at": None if frame is None else frame["received_at"],
            "frame_age_s": None if frame is None else max(0., now - frame["received_at"]),
            "frame_max_age_s": FRAME_MAX_AGE, "error_x": self._error_x,
            "yaw": self._yaw, "yaw_limit": YAW_LIMIT, "deadband": DEADBAND,
        }

    def state(self, now):
        """Read a snapshot, expiring stale selection without renewing any image."""
        now, _ = self._clock(now)
        self._update(now)
        return self._snapshot(now)
