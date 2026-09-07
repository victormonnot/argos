"""Manual flight, expressed in the same type as the guidance law.

The operator sends a normalised intention and it becomes an attitude here. The point
is not convenience: it is that the result leaves through the same door as the
tracking command, so the proximity guard applies to a human exactly as it applies to
the law. The defect this arrangement replaces was a guard living inside the tracking
branch, which let an operator push the aircraft toward the very target the autonomy
was refusing to approach.

``yaw`` is an offset to the current heading, per command, like the one the law
produces. Never an absolute heading: the compass is not trustworthy enough to hold
one, so a commanded heading would drift away from the real one.
"""
from __future__ import annotations

import math

from argos.core import AttitudeCmd, Command, CommandSource

DEG = math.pi / 180.0


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def operator_command(
    now: float,
    forward: float = 0.0,
    right: float = 0.0,
    up: float = 0.0,
    yaw: float = 0.0,
    max_tilt: float = 12.0 * DEG,
    max_dyaw: float = 5.0 * DEG,
) -> Command:
    """A stick intention in -1..1 per axis, as an attitude command stamped ``now``.

    ``up`` maps onto thrust around the neutral 0.5, so a centred stick holds altitude
    rather than commanding a climb. The limits here mirror the law's own authority;
    like the law's, they are not a safety property. :mod:`argos.safety` bounds this
    command exactly as it bounds every other.
    """
    return Command(
        payload=AttitudeCmd(
            roll=_clamp(right, -1.0, 1.0) * max_tilt,
            pitch=-_clamp(forward, -1.0, 1.0) * max_tilt,   # advancing is nose down
            dyaw=_clamp(yaw, -1.0, 1.0) * max_dyaw,
            thrust=_clamp(0.5 + 0.5 * _clamp(up, -1.0, 1.0), 0.0, 1.0),
        ),
        source=CommandSource.OPERATOR,
        t=now,
    )
