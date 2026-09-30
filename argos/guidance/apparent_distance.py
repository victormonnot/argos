"""Small experimental pitch requests from apparent person height, not metres.

Positive output requests forward stick. Receiver direction must be checked on
the actual model. The caller owns identity, freshness and radio authority.
"""
import math


PITCH_LIMIT = .05
GAIN = .18
DAMPING = .10


def geometry(box):
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)):
        raise ValueError("Distance needs a finite normalized box")
    x, y, w, h = box
    if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1
            and x + w <= 1.000001 and y + h <= 1.000001):
        raise ValueError("Invalid distance box")
    usable = (x >= .015 and y >= .015 and x + w <= .985 and y + h <= .985
              and .08 <= h <= .85 and abs(2 * (x + w / 2 - .5)) <= .35
              and abs(2 * (y + h / 2 - .5)) <= .65)
    return h, w / h, usable


class ApparentDistanceLaw:
    def __init__(self):
        self.reference = self.reference_aspect = self.at = None
        self.height = self.rate = self.value = 0.
        self.valid = False
        self.reason = "not_selected"
        self._last_height = None
        self._confirm = 0

    def start(self, box, at):
        h, aspect, usable = geometry(box)
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
            raise ValueError("Invalid image receipt time")
        if not usable:
            raise ValueError("Distance needs a fully visible person near the image centre")
        self.reference, self.reference_aspect = h, aspect
        self.at, self.height, self._last_height = at, math.log(h), h
        self.rate = self.value = 0.
        self.valid, self.reason, self._confirm = True, "reference_captured", 2

    def pause(self, reason="target_unavailable"):
        self.value = self.rate = 0.
        self.valid, self.reason, self._confirm = False, reason, 0

    def update(self, box, at):
        h, aspect, usable = geometry(box)
        if (self.reference is None or type(at) not in (int, float)
                or not math.isfinite(at) or at < self.at):
            raise ValueError("Distance image time regressed or reference is absent")
        if at == self.at:
            return self.value  # Repeated reads never advance filters or confirmation.
        dt = at - self.at
        jump = self._last_height is not None and abs(math.log(h / self._last_height)) > math.log(1.3)
        self.at, self._last_height = at, h
        if (not usable or not .7 <= aspect / self.reference_aspect <= 1.3
                or jump or dt > .45):
            self.height = math.log(h)
            self.pause("box_geometry" if not usable else "size_or_pose_change")
            return 0.
        if not self.valid:
            self._confirm += 1
            self.height = math.log(h)
            self.rate = self.value = 0.
            if self._confirm < 2:
                self.reason = "confirming_box"
                return 0.
            self.valid = True
        previous = self.height
        self.height += -math.expm1(-dt / .15) * (math.log(h) - self.height)
        measured = max(-1., min(1., (self.height - previous) / max(dt, .05)))
        self.rate += -math.expm1(-dt / .2) * (measured - self.rate)
        error = math.log(self.reference) - self.height
        error = math.copysign(max(0., abs(error) - .06), error)
        requested = max(-PITCH_LIMIT, min(PITCH_LIMIT, GAIN * error - DAMPING * self.rate))
        step = .10 * min(dt, .15)
        self.value += max(-step, min(step, requested - self.value))
        self.reason = "tracking_size"
        return self.value
