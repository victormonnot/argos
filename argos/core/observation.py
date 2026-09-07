"""What an agent knows about itself and perceives around it.

Three decisions carry this module.

**Perception is not communication.** An :class:`Observation` holds what the agent
*sees*. What it knows *because another agent told it* travels on a link and never
appears here. Conflating the two is the classic way a swarm simulation cheats:
global state gets read under the name of "neighbourhood", and the day the radio is
cut nothing degrades, because nothing was ever going through the radio. Keeping
them apart is what makes a link-loss measurement mean anything.

**"Believes" is the operative word in** :class:`SelfState`. Under GNSS denial
``pos`` is a noisy estimate, not a fact, and ``pos_cov`` says how noisy. The field
exists from the first commit because the degradation harness writes into it; it is
``None`` for as long as nothing is degrading position.

**The target belongs here.** ``World.observe()`` returns everything the agent
perceives, and a designated target is perceived, by the camera. Leaving it out
would force the guidance layer to collect its inputs from two different places,
which is exactly the coupling this interface exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from .target import TargetView

AgentId = int


@dataclass(frozen=True)
class SelfState:
    """What the agent believes about itself.

    ``armed`` is a fact reported by the autopilot. Whether the aircraft is
    *flying* is a judgement built on top of it, and deliberately not stored here:
    it depends on a threshold, and thresholds are policy. The safety layer owns
    that decision so it is stated in one place rather than assumed in several.

    **Frame and units, stated once so nothing has to infer them.** ``pos`` and
    ``vel`` are metres and metres per second in a local East-North-Up frame whose
    origin is the takeoff point, so ``pos[2]`` is height above takeoff and is
    positive upward. This is not height above the ground: over a slope or a wall
    the two differ, and no consumer of this type may assume otherwise.

    **Dimensions.** ``pos`` and ``vel`` are both ``(2,)`` or both ``(3,)``, and they
    match the ``dim`` of the world they came from. Two dimensions have no altitude
    at all, which changes what a consumer can ask: a 2-D state cannot answer a
    question about height, and code that needs one has to say what it does instead.

    **Immutability is shallow, and the producer is what closes the gap.** The
    dataclass is frozen, but a NumPy array inside a frozen dataclass is still
    writable, so a consumer holding a state could reach back into whatever produced
    it and move the aircraft. Now that something exists which hands states out, the
    rule is stated rather than left open: **a producer that does not own these
    arrays outright hands over copies, and marks the copies read-only.** A consumer
    may read them freely and must never write to them. The first backend does this
    and is tested for it.
    """

    t: float
    """When this state was observed, on the world clock (:meth:`World.time`).

    Not :func:`time.monotonic`: in simulation the wall clock is unrelated to
    simulated time, and comparing the two silently mixes two axes.
    """

    pos: np.ndarray                      # (2,) or (3,), metres, ENU from takeoff
    vel: np.ndarray                      # (2,) or (3,), m/s, same frame
    yaw: float = 0.0
    """rad, the heading actually measured, as a compass bearing in the frame above.

    Zero points North, along ``+y``, and it grows turning right, so a quarter turn
    to the right is ``+pi/2`` and points East. The zero is pinned here because it is
    the kind of convention two backends can pick differently while each looks
    correct on its own, and the disagreement then shows up as an aircraft that flies
    ninety degrees off with nothing obviously wrong anywhere.
    """
    armed: bool = False                  # motors armed, per the autopilot
    alive: bool = True                   # still in the swarm; attrition clears it
    pos_cov: np.ndarray | None = None    # (d, d), None while position is not degraded


@dataclass(frozen=True)
class Neighbor:
    """Another agent, as *perceived*. Not an agent one communicates with.

    The fields are the ones a sensor actually gives: relative position and relative
    velocity. Identity is not guaranteed, and ``agent`` is ``None`` whenever the
    sensor sees a moving object without knowing which one it is. That is the normal
    case in flight, and reciprocal collision avoidance is content with it, so the
    type does not pretend otherwise.
    """

    pos: np.ndarray            # relative position, metres
    vel: np.ndarray            # relative velocity, m/s
    radius: float = 0.0        # metres, the neighbour's assumed extent
    agent: AgentId | None = None


@dataclass(frozen=True)
class Observation:
    """Everything one agent perceives at one instant, and nothing else."""

    me: SelfState
    neighbors: Sequence[Neighbor] = field(default_factory=tuple)
    target: TargetView | None = None
