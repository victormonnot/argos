"""Image-only framing requests for the explicit digital-control profiles.

This law requests pilot axes, not velocities or a position hold. The caller owns
authority, observation freshness, target association and immediate cancellation.
The profile must disable RC1..4 and AltHold throttle deadzones: small numerical
commands otherwise disappear before reaching the autopilot's controllers.

Yaw turns toward the image error; optional vertical translation corrects its
vertical component. Pilot-throttle framing never requests vertical translation.
Forward pitch regulates apparent height with damping from changing
image size, without metric range or body-size assumptions. A changing human pose
can still change this measurement without changing range.
"""
from __future__ import annotations

import math
from numbers import Real


MIN_REFERENCE_HEIGHT = .08
MAX_REFERENCE_HEIGHT = .45
REFERENCE_STEP = 1.1

YAW_GAIN = 1.0
UP_GAIN = .8
FORWARD_GAIN = .35
FORWARD_DAMPING = 2.0
YAW_DEADBAND = .035
UP_DEADBAND = .08
ALIGNMENT_LIMIT = .25
HEIGHT_FILTER_SECONDS = .4
DERIVATIVE_FILTER_SECONDS = .5
MAX_LOG_HEIGHT_RATE = 1.0
MAX_SLEW_INTERVAL = .25
AXIS_LIMITS = {"forward": .35, "right": 0., "up": .3, "yaw": .5}
AXIS_SLEW_PER_SECOND = {"forward": .35, "up": .4, "yaw": .9}


def _finite(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _observation(box, received_at) -> tuple[list[float], float]:
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(_finite(value) and 0 <= value <= 1 for value in box)
            or box[2] <= 0 or box[3] <= 0
            or box[0] + box[2] > 1 + 1e-9 or box[1] + box[3] > 1 + 1e-9):
        raise ValueError("framing needs a finite normalized box with positive dimensions")
    if not _finite(received_at) or received_at < 0:
        raise ValueError("framing needs a finite nonnegative camera-receipt timestamp")
    return [float(value) for value in box], float(received_at)


def _errors(box) -> tuple[float, float]:
    return 2 * (box[0] + box[2] / 2 - .5), 2 * (box[1] + box[3] / 2 - .5)


def _forward_usable(box, error_x, error_y) -> bool:
    return (
        abs(error_x) < ALIGNMENT_LIMIT and abs(error_y) < ALIGNMENT_LIMIT
        and box[0] > 1e-9 and box[1] > 1e-9
        and box[0] + box[2] < 1 - 1e-9 and box[1] + box[3] < 1 - 1e-9
    )


def _deadband(value: float, band: float) -> float:
    return math.copysign(max(0., abs(value) - band), value)


def _clip(value: float, limit: float) -> float:
    return min(limit, max(-limit, value))


class FramingLaw:
    """A serial consumer of distinct image observations on one local clock.

    At the default 300-unit MAVLink cap and 20-degree lean limit, a forward axis
    of .35 requests approximately 2.1 degrees of pitch. That body-camera tilt
    alone shifts normalized vertical error by about .072 with the fixed optics;
    the .08 vertical deadband avoids chasing most of that immediate coupling.
    It does not compensate arbitrary camera mounts or large aircraft motion.

    Height and its derivative are filtered on new images, never on repeated
    20 Hz transmission ticks. Derivative is on the measurement, so changing the
    reference does not create a derivative kick. There is no integral term.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.reference_height: float | None = None
        self.height: float | None = None
        self.error_x = self.error_y = 0.
        self._received_at: float | None = None
        self._log_height = 0.
        self._height_rate = 0.
        self._height_usable = False
        self._vertical_control = True
        self._axes = dict.fromkeys(AXIS_LIMITS, 0.)

    def start(self, box: list, received_at: float, *, vertical_control=True) -> None:
        box, received_at = _observation(box, received_at)
        if type(vertical_control) is not bool:
            raise ValueError("vertical control must be explicitly enabled or disabled")
        if not MIN_REFERENCE_HEIGHT <= box[3] <= MAX_REFERENCE_HEIGHT:
            raise ValueError("initial target height must be between 8% and 45% of the image")
        # Validation precedes reset: a bad request cannot corrupt an active law.
        self.reset()
        self._vertical_control = vertical_control
        self.reference_height = self.height = box[3]
        self.error_x, self.error_y = _errors(box)
        self._received_at = received_at
        self._log_height = math.log(box[3])
        self._height_usable = _forward_usable(box, self.error_x, self.error_y)

    def pause(self) -> None:
        """Neutralize output while retaining the operator's chosen reference.

        No image is processed or predicted here. The last accepted timestamp
        still rejects repeated observations, but the next new image starts a
        fresh height derivative history and slews from zero. The caller owns
        the pause deadline and decides whether that image may resume framing.
        """
        self._axes = dict.fromkeys(AXIS_LIMITS, 0.)
        self._log_height = self._height_rate = 0.
        self._height_usable = False

    def adjust(self, direction: str) -> float:
        if self.reference_height is None:
            raise RuntimeError("framing law is not started")
        if direction not in ("closer", "farther"):
            raise ValueError("framing adjustment must be closer or farther")
        factor = REFERENCE_STEP if direction == "closer" else 1 / REFERENCE_STEP
        self.reference_height = min(
            MAX_REFERENCE_HEIGHT, max(MIN_REFERENCE_HEIGHT, self.reference_height * factor),
        )
        return self.reference_height

    def update(self, box: list, received_at: float) -> dict[str, float]:
        box, received_at = _observation(box, received_at)
        if self._received_at is None or self.reference_height is None:
            raise RuntimeError("framing law is not started")
        if received_at <= self._received_at:
            raise ValueError("framing frame timestamps must strictly increase")
        dt = received_at - self._received_at
        error_x, error_y = _errors(box)
        height_usable = _forward_usable(box, error_x, error_y)
        log_height = math.log(box[3])
        if height_usable and self._height_usable:
            height_alpha = -math.expm1(-dt / HEIGHT_FILTER_SECONDS)
            filtered_height = self._log_height + height_alpha * (log_height - self._log_height)
            # A tiny timestamp increment cannot overflow a derivative or bypass
            # the output slew limit. Acquisition normally runs near 5 Hz.
            measured_rate = _clip(
                (filtered_height - self._log_height) / max(dt, .05), MAX_LOG_HEIGHT_RATE,
            )
            rate_alpha = -math.expm1(-dt / DERIVATIVE_FILTER_SECONDS)
            self._height_rate += rate_alpha * (measured_rate - self._height_rate)
            self._log_height = filtered_height
        else:
            # Clipping and large pointing changes make size change unreliable.
            # Re-entering alignment starts a new derivative history.
            self._log_height, self._height_rate = log_height, 0.
        size_error = math.log(self.reference_height) - self._log_height
        requested = {
            "forward": (FORWARD_GAIN * size_error - FORWARD_DAMPING * self._height_rate
                        if height_usable else 0.),
            "right": 0.,
            "up": -UP_GAIN * _deadband(error_y, UP_DEADBAND) if self._vertical_control else 0.,
            "yaw": YAW_GAIN * _deadband(error_x, YAW_DEADBAND),
        }
        for axis, rate in AXIS_SLEW_PER_SECOND.items():
            target = _clip(requested[axis], AXIS_LIMITS[axis])
            change = _clip(target - self._axes[axis], rate * min(dt, MAX_SLEW_INTERVAL))
            self._axes[axis] += change
        if not height_usable:
            self._axes["forward"] = 0.  # Inhibition never waits for the slew limiter.
        self.height = box[3]
        self.error_x, self.error_y = error_x, error_y
        self._height_usable, self._received_at = height_usable, received_at
        return dict(self._axes)
