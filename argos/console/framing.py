"""Image-framing lifecycle; vehicle authority remains with FlightControl.

Only server-owned image observations enter this helper. It neither knows the
vehicle transport nor renews browser authority. Receipt timestamps use the
session's local monotonic clock; they are not camera exposure timestamps.
"""
from __future__ import annotations

import math
from copy import deepcopy

from argos.guidance.image_framing import FramingLaw


FRAME_MAX_AGE = .45
DETECTION_PAUSE = .35
TAKEOVER_TIMEOUT = 2.
EDGE_MARGIN = .005
MIN_CONFIDENCE = .5
ENGAGE_HEIGHT = (.08, .45)
ACTIVE_HEIGHT = (.06, .65)
AXES = ("forward", "right", "up", "yaw")
# Independent acceptance bounds at the lifecycle/flight-command boundary.
# A broken guidance producer must not expand its own control authority.
OUTPUT_LIMITS = {"forward": .35, "right": 0., "up": .3, "yaw": .5}
DIAGNOSTIC_REASON_LIMIT = 1024


def _zero():
    return dict.fromkeys(AXES, 0.)


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _time(value):
    if not _number(value) or value < 0:
        raise ValueError("Framing requires a finite nonnegative session time")
    return float(value)


class FramingControl:
    """A selected target, explicit engagement and a latched takeover deadline.

    The caller checks lease, vehicle and pilot-input conditions before engage.
    It calls tick before sending a command, and invokes its existing landing
    path when takeover_due becomes true. An isolated detection miss permits a
    short neutral pause; only the same target may recover before its deadline.
    Snapshot reads never renew deadlines, advance the guidance law or change
    lifecycle state.
    """

    def __init__(self, enabled=False):
        self.enabled = bool(enabled)
        self.revision = 0
        self.phase = "idle" if self.enabled else "disabled"
        self._observation = None
        self._observation_error = "No recent analyzed image"
        self._target_id = None
        self._context = None
        self._last_now = None
        self._last_sequence = self._last_received_at = None
        self._deadline = None
        self._pause_deadline = None
        self._pause_issue = ""
        self._pause_evidence = None
        self._last_loss = None
        self._reason = "Select a person in a recent analyzed image"
        self._output = _zero()
        self._law = FramingLaw()

    @property
    def last_loss(self):
        """Frozen first-takeover evidence, copied for each consumer."""
        return deepcopy(self._last_loss)

    def _evidence(self, now):
        observation = self._observation
        return {"evidence_at": now,
                "sequence": None if observation is None else observation["sequence"],
                "frame_age_s": None if observation is None else
                max(0., now - observation["received_at"]),
                # observe() already bounds this to 16 identities and four
                # normalized coordinates per box; never retain image pixels.
                "detections": [] if observation is None else
                deepcopy(observation["detections"])}

    def observe(self, observation: dict | None):
        """Replace the current observation without sending or renewing anything.

        Copy the small metadata payload so later caller mutation cannot alter a
        selected target. Invalid input behaves as unavailable perception.
        """
        previous = self._observation
        self._observation = None
        self._observation_error = "No recent analyzed image"
        if observation is None:
            return
        try:
            if not isinstance(observation, dict):
                raise ValueError("expected image metadata")
            context = observation["run_id"], observation["video_id"]
            sequence = observation["sequence"]
            received_at = _time(observation["received_at"])
            if (any(not isinstance(item, str) or not item for item in context)
                    or type(sequence) is not int or sequence < 0):
                raise ValueError("invalid image identity")
            incoming = observation["detections"]
            if not isinstance(incoming, list) or len(incoming) > 16:
                raise ValueError("invalid detection list")
            detections, identities = [], set()
            for detection in incoming:
                if not isinstance(detection, dict):
                    raise ValueError("invalid person detection")
                identity = detection["track_id"]
                box, confidence = detection["box"], detection["confidence"]
                if (type(identity) is not int or identity < 1 or identity in identities
                        or not isinstance(box, (list, tuple)) or len(box) != 4
                        or any(not _number(v) or not 0 <= v <= 1 for v in box)
                        or box[2] <= 0 or box[3] <= 0
                        or box[0] + box[2] > 1.000001 or box[1] + box[3] > 1.000001
                        or not _number(confidence) or not 0 <= confidence <= 1):
                    raise ValueError("invalid person bounds, confidence or identity")
                identities.add(identity)
                detections.append({"track_id": identity, "box": list(map(float, box)),
                                   "confidence": float(confidence)})
            candidate = {"context": context, "sequence": sequence,
                         "received_at": received_at, "detections": detections}
            if (previous is not None and previous["context"] == context
                    and previous["sequence"] == sequence and previous != candidate):
                self._observation_error = "Analyzed image identity changed"
                return
            self._observation = candidate
            self._observation_error = ""
        except (KeyError, TypeError, ValueError, OverflowError):
            self._observation_error = "Invalid analyzed image metadata"

    def _target(self, now, *, limits=None):
        """Return (detection, issue), without changing any state."""
        observation = self._observation
        if observation is None:
            return None, self._observation_error
        age = now - observation["received_at"]
        if not 0 <= age <= FRAME_MAX_AGE:
            return None, "Selected target image is stale"
        if self._context != observation["context"]:
            return None, "Camera source changed; select the person again"
        if self.phase == "active" and self._last_sequence is not None:
            if (observation["sequence"] < self._last_sequence
                    or observation["received_at"] < self._last_received_at):
                return None, "Analyzed image order changed"
            if (observation["sequence"] == self._last_sequence
                    and observation["received_at"] != self._last_received_at):
                return None, "Analyzed image identity changed"
        detections = observation["detections"]
        target = next((item for item in detections if item["track_id"] == self._target_id), None)
        if target is None:
            return None, ("Selected target was lost; only different person IDs are detected"
                          if detections else "No person detection in the analyzed image")
        if limits is not None:
            if len(detections) != 1:
                return None, "Framing requires exactly one visible person"
            x, y, width, height = target["box"]
            if (x < EDGE_MARGIN or y < EDGE_MARGIN
                    or x + width > 1 - EDGE_MARGIN or y + height > 1 - EDGE_MARGIN):
                return None, "Selected person is too close to the image edge"
            if not limits[0] <= height <= limits[1]:
                return None, "Selected person is outside the supported image-size range"
            if target["confidence"] < MIN_CONFIDENCE:
                return None, "Selected target confidence is too low"
        return target, ""

    def _pause_allowed(self, issue):
        # _target already rejects source/order/staleness/geometry problems.
        # A different ID is never a substitute for the explicitly selected one.
        return (issue == "Selected target confidence is too low"
                or (issue == "No person detection in the analyzed image" and self._observation is not None
                    and not self._observation["detections"]))

    def _pause(self, now, issue):
        self._output = _zero()
        if self._pause_deadline is None:
            try:
                self._law.pause()
            except Exception:
                self._takeover(now, "Image framing pause failed; take manual control")
                return
            self._pause_deadline = now + DETECTION_PAUSE
            self._pause_issue = issue
            self._pause_evidence = self._evidence(now)
            self._reason = f"Image framing paused; {issue}; neutral input while waiting briefly for the selected person"
            self.revision += 1
        # Retain the latest bad image's identity too: an older good image must
        # never recover a pause or restore the command from before the gap.
        self._last_sequence = self._observation["sequence"]
        self._last_received_at = self._observation["received_at"]

    def select(self, track_id, context: tuple[str, str], now):
        now = _time(now)
        if not self.enabled:
            raise RuntimeError("Image framing is disabled")
        if self.phase in ("active", "takeover"):
            raise RuntimeError("Stop framing or take manual control before selecting another person")
        if (type(track_id) is not int or track_id < 1
                or not isinstance(context, tuple) or len(context) != 2
                or any(not isinstance(item, str) or not item for item in context)):
            raise ValueError("A target identity and camera context are required")
        observation = self._observation
        if (observation is None or observation["context"] != context
                or not 0 <= now - observation["received_at"] <= FRAME_MAX_AGE
                or not any(item["track_id"] == track_id for item in observation["detections"])):
            raise RuntimeError("The selected person is no longer present in a recent image")
        self._target_id, self._context = track_id, context
        self._last_now = now
        self._reset_output()
        self._last_loss = None
        self.phase = "selected"
        self._reason = "Person selected"
        self.revision += 1

    def engage(self, now):
        now = _time(now)
        if not self.enabled or self.phase != "selected":
            raise RuntimeError("Select a person before starting image framing")
        target, issue = self._target(now, limits=ENGAGE_HEIGHT)
        if issue:
            raise RuntimeError(issue)
        observation = self._observation
        self._law.start(target["box"], observation["received_at"])
        self._output = _zero()
        self._last_sequence = observation["sequence"]
        self._last_received_at = observation["received_at"]
        self._last_now = now
        self._deadline = None
        self._pause_deadline = None
        self._pause_issue = ""
        self._pause_evidence = None
        self._last_loss = None
        self.phase = "active"
        self._reason = "Image framing active"
        self.revision += 1

    def _reset_output(self):
        self._law.reset()
        self._output = _zero()
        self._last_sequence = self._last_received_at = None
        self._deadline = None
        self._pause_deadline = None
        self._pause_issue = ""
        self._pause_evidence = None

    def _takeover(self, now, reason, *, evidence=None):
        if self._last_loss is None:
            # For pause expiry, evidence belongs to the first bad image, while
            # at remains the actual takeover time. A late good image therefore
            # cannot be misrepresented as the image which caused the failure.
            self._last_loss = {"at": now, "reason": str(reason)[:DIAGNOSTIC_REASON_LIMIT],
                               "target_id": self._target_id,
                               **deepcopy(evidence if evidence is not None else self._evidence(now))}
        self._reset_output()
        self.phase = "takeover"
        self._deadline = now + TAKEOVER_TIMEOUT
        self._reason = reason
        self.revision += 1

    def tick(self, now, vehicle_reason=""):
        now = _time(now)
        self._last_now = now
        if self.phase != "active":
            return
        # Expiry wins even if a good image arrives on this exact tick. Once
        # latched, the normal explicit-takeover path cannot resume itself.
        if self._pause_deadline is not None and now >= self._pause_deadline:
            self._takeover(now, f"{self._pause_issue}; selected target did not recover during the framing pause; take manual control",
                           evidence=self._pause_evidence)
            return
        target, issue = self._target(now, limits=ACTIVE_HEIGHT)
        if vehicle_reason or issue:
            if not vehicle_reason and self._pause_allowed(issue):
                self._pause(now, issue)
            else:
                self._takeover(now, str(vehicle_reason or issue))
            return
        observation = self._observation
        sequence, received_at = observation["sequence"], observation["received_at"]
        if sequence < self._last_sequence or received_at < self._last_received_at:
            self._takeover(now, "Analyzed image order changed")
            return
        if sequence == self._last_sequence or received_at == self._last_received_at:
            return
        try:
            output = self._law.update(target["box"], received_at)
            if (not isinstance(output, dict) or set(output) != set(AXES)
                    or any(not _number(output[key]) or abs(output[key]) > limit
                           for key, limit in OUTPUT_LIMITS.items())):
                raise ValueError("invalid framing output")
            self._output = {key: float(output[key]) for key in AXES}
        except Exception:
            self._takeover(now, "Image framing calculation failed; take manual control")
            return
        self._last_sequence, self._last_received_at = sequence, received_at
        if self._pause_deadline is not None:
            self._pause_deadline = None
            self._pause_issue = ""
            self._pause_evidence = None
            self._reason = "Image framing active"
            self.revision += 1

    def stop(self, reason="Manual control"):
        """Explicit pilot acknowledgement, after the caller's current tick.

        Without a separate time argument, selection freshness uses the last
        select/engage/tick/adjust clock. FlightControl ticks before pilot actions.
        """
        target = None
        if self._last_now is not None:
            target, _ = self._target(self._last_now, limits=ENGAGE_HEIGHT)
        self._reset_output()
        if target is None:
            self._target_id = self._context = None
        self.phase = ("selected" if self._target_id is not None else "idle") if self.enabled else "disabled"
        self._reason = str(reason)
        self.revision += 1

    def clear(self, reason="Selection cleared", *, reset_loss=False):
        self._reset_output()
        if reset_loss:
            self._last_loss = None
        self._target_id = self._context = None
        self.phase = "idle" if self.enabled else "disabled"
        self._reason = str(reason)
        self.revision += 1

    def adjust(self, direction, now):
        now = _time(now)
        # Evaluate loss before handling an operator adjustment. It must not
        # refresh a lost-target deadline or conceal unavailable perception.
        self.tick(now)
        if self.phase != "active":
            raise RuntimeError("Image framing must be active before adjusting its size")
        if self._pause_deadline is not None:
            raise RuntimeError("Image framing is paused; wait for the selected person before adjusting its size")
        if direction not in ("closer", "farther"):
            raise ValueError("Choose closer or farther")
        try:
            self._law.adjust(direction)
        except Exception:
            self._takeover(now, "Image framing adjustment failed; take manual control")
            raise RuntimeError(self._reason) from None
        self.revision += 1

    def takeover_due(self, now):
        now = _time(now)
        return self.phase == "takeover" and self._deadline is not None and now >= self._deadline

    def axes(self, now):
        now = _time(now)
        if (self.phase != "active" or self._pause_deadline is not None
                or self._target(now, limits=ACTIVE_HEIGHT)[1]):
            return _zero()
        return dict(self._output)

    def state(self, now, vehicle_reason=""):
        now = _time(now)
        _, issue = self._target(now, limits=ENGAGE_HEIGHT)
        available = (self.enabled and self.phase == "selected" and not issue
                     and not vehicle_reason)
        if not self.enabled:
            reason = "Image framing is disabled"
        elif self.phase == "takeover":
            reason = self._reason
        elif self._pause_deadline is not None:
            reason = str(vehicle_reason or self._reason)
        elif self.phase in ("selected", "active"):
            limits = ACTIVE_HEIGHT if self.phase == "active" else ENGAGE_HEIGHT
            reason = str(vehicle_reason or self._target(now, limits=limits)[1] or self._reason)
        else:
            reason = self._reason
        observation = self._observation
        age = None if observation is None else max(0., now - observation["received_at"])
        return {"enabled": self.enabled, "revision": self.revision, "phase": self.phase,
                "active": self.phase == "active", "paused": self._pause_deadline is not None,
                "target_id": self._target_id,
                "available": available, "reason": reason,
                "error_x": self._law.error_x if self._pause_deadline is None else None,
                "error_y": self._law.error_y if self._pause_deadline is None else None,
                "height": self._law.height if self._pause_deadline is None else None,
                "reference_height": self._law.reference_height,
                "axes": self.axes(now), "frame_age_s": age,
                "last_loss": self.last_loss,
                "takeover_remaining_s": max(0., self._deadline - now)
                if self.phase == "takeover" and self._deadline is not None else None}
