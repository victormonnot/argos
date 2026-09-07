"""What the vision layer knows about a target.

Two types, and the split between them matters.

A :class:`Detection` is one object a detector found in one frame. A
:class:`TargetView` is what the guidance law consumes: the single locked target,
which may have been detected on this frame or may be coasting through an
occlusion. Collapsing the two would make "the detector saw nothing this frame"
indistinguishable from "there is no target", and those call for opposite
behaviour: the first means keep flying on the last estimate for a moment, the
second means stop.

Both are normalised to the image and carry no pixels. The guidance law must not
know the camera's resolution, and these numbers describe the same geometry whether
the frame came from a simulator, a degraded analog downlink or a CSI sensor.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Detection:
    """One object found by a detector in one frame.

    ``label`` is whatever the detector called it. Mapping detector classes onto the
    operator's vocabulary is a perception concern and does not belong here.
    """

    cx: float           # 0..1, box centre as a fraction of image width
    cy: float           # 0..1, box centre as a fraction of image height
    w: float            # 0..1, box width as a fraction of image width
    h: float            # 0..1, box height as a fraction of image height
    confidence: float   # 0..1, as reported by the detector
    label: str = ""     # the detector's own class name


@dataclass(frozen=True)
class TargetView:
    """The locked target as the guidance law sees it.

    ``size`` is the interesting field. There is no range sensor on this aircraft,
    so distance is inferred from apparent size: the box grows as the aircraft
    closes. That makes any threshold on ``size`` a **calibration and not a
    constant**, because the reachable size is capped by geometry. An aircraft at
    altitude ``alt`` above a target of height ``h`` can never see better than

        size_max ~= h / (2 * alt * tan(half vertical field of view))

    which is around 0.15 for a standing person seen from 12 m with a 0.45 rad half
    angle. A threshold set above that ceiling never fires at all: the aircraft does
    not slow down, and it overflies the target instead of holding station. The
    failure is silent, which is why it is written here rather than in a comment
    next to whichever constant happens to be wrong.

    ``found`` and ``age`` together say how much to trust the rest. ``has`` without
    ``found`` means the target is being coasted from its last detection, and ``age``
    says for how long. Coasting past a few hundred milliseconds is a guess, and the
    guidance law is expected to slow down rather than act on it confidently.

    **``age`` and ``t`` are two different facts and both are needed.** ``age`` is
    how old the detection was *when this view was computed*; ``t`` is when that
    computation happened. A stored age does not grow on its own: a view built once
    and then handed around reports the same ``age`` forever, so a consumer that
    reads ``age`` alone cannot tell a fresh view from one produced by a perception
    loop that stopped running. The age at any later instant is
    ``age + (now - t)``, and a consumer that cares about staleness must compute it.
    """

    has: bool = False          # a target is locked: detected, or coasting
    found: bool = False        # detected on THIS frame, as opposed to coasting
    error_x: float = 0.0       # -1..1, horizontal offset from image centre
    error_y: float = 0.0       # -1..1, vertical offset from image centre
    size: float = 0.0          # 0..1, box height over image height
    age: float = 0.0           # seconds since the last real detection, as of `t`
    confidence: float = 0.0    # 0..1, from the detection this view is based on
    t: float | None = None
    """When this view was computed, on the world clock (:meth:`argos.core.World.time`).

    ``None`` means unstamped, and a consumer enforcing freshness has to treat that
    as unusable rather than as fresh: it cannot age ``age`` without knowing when the
    age was taken.
    """
