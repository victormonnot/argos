"""The hard bounds, and the clipping that enforces them.

These are **not** the gains of a control law. A control law can be wrong: badly
tuned, fed a bad detection, or replaced tomorrow by a learned policy nobody can
read. These bounds hold anyway. That is the whole reason they live in a separate
module from anything that computes a command.

Bounds are per command space and they have to be, because the quantities are not
comparable: 3 m/s, 4 m/s2, 1 rad/s and 70 % of collective thrust cannot share a
limit. Anything that clips a command therefore has to know which space it is in,
which is why :class:`argos.core.Command` carries that fact rather than leaving it
to be inferred.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from argos.core import AccelCmd, AttitudeCmd, CtbrCmd, Payload, VelocityCmd

DEG = math.pi / 180.0


def clamp(value: float, low: float, high: float) -> float:
    """``value`` brought inside ``[low, high]``."""
    return low if value < low else (high if value > high else value)


@dataclass(frozen=True)
class Envelope:
    """Everything a command is allowed to be, per space.

    The defaults are deliberately timid. Widening them is a decision to be made
    against a measurement, not a knob to turn when the aircraft feels sluggish.
    """

    # --- ATTITUDE ---------------------------------------------------------
    max_tilt: float = 15.0 * DEG     # rad, absolute bank and pitch, every path
    max_dyaw: float = 8.0 * DEG      # rad, heading offset per command
    thrust_min: float = 0.30         # 0.5 holds altitude, so this bounds descent
    thrust_max: float = 0.70         # ... and this bounds climb

    # --- VELOCITY and ACCEL -----------------------------------------------
    max_speed: float = 3.0           # m/s, per axis
    max_accel: float = 4.0           # m/s2, per axis
    max_yaw_rate: float = 1.0        # rad/s

    # --- proximity --------------------------------------------------------
    size_stop: float = 0.12
    """Apparent target size at or above which forward *command* components are removed.

    **What this enforces:** above the threshold, the component of the commanded
    attitude, velocity or acceleration that closes the distance is set to zero,
    whatever the source. It applies to the operator as much as to the guidance law,
    because a guard that only covers the autonomous path leaves the manual path
    able to fly into what the autonomy refused to approach.

    **What this does not establish:** a minimum physical distance. Zeroing a
    forward command does not stop momentum already acquired, says nothing about the
    other axes, and says nothing about a target that is itself moving. Whether a
    separation is actually maintained is a question about dynamics, and it has to
    be measured with a moving simulation and an independent reference. Nothing in
    this package has measured it.

    It is also a **geometric calibration, not a constant**. Apparent size is capped
    by the viewing geometry, so a threshold set above what the camera can ever see
    never fires at all. See :class:`argos.core.TargetView` for the ceiling.
    """

    # --- flight state -----------------------------------------------------
    min_flying_alt: float = 0.8
    """Height above the takeoff point, in metres, past which an armed aircraft
    counts as flying. Height above *takeoff*, not above the ground: see
    :class:`argos.core.SelfState` for the frame."""

    # --- freshness --------------------------------------------------------
    max_command_age: float = 0.5
    """Seconds a command stays usable after it was produced.

    A **policy**, configurable and tested as such. It is not a measured property of
    any loop: nothing here establishes that a command older than this is dangerous
    or that a newer one is safe. It exists so that a control layer which has
    stopped producing cannot keep an old intent alive by accident.
    """

    max_state_age: float = 0.5
    """Seconds an observed state stays usable. Same standing as
    :attr:`max_command_age`: a policy, not a measurement."""

    max_target_age: float = 1.0
    """Seconds since the last real detection past which a target view is refused.

    Longer than the state limit on purpose: coasting through a brief occlusion is a
    designed behaviour, whereas an aircraft state that old means the estimator has
    stopped. Computed as ``age + (now - t)``, because a stored age does not grow on
    its own.
    """

    max_clock_skew: float = 0.0
    """Seconds an input may be stamped *ahead* of the world clock before it is refused.

    **Zero, and that is the policy rather than an oversight.** Everything on one
    world reads one clock (:meth:`argos.core.World.time`), and that clock only moves
    forward, so a timestamp in the future cannot be produced by any correct caller.
    It means a second time base leaked in, and the age computed from it is not small,
    it is meaningless: an input stamped far ahead reads as freshly produced, and a
    target's declared detection age is cancelled out by a negative elapsed term.

    It is a field rather than a constant so that a backend which genuinely has two
    clocks -- agents on separate machines, whose monotonic clocks share no origin --
    can widen it deliberately and write down why. Widening it without that reason
    reintroduces exactly the hole it is here to close.

    **What stays open if it is ever widened.** Raising a threshold does not make two
    clocks of different origins comparable; it only decides how much disagreement to
    ignore. A positive tolerance needs a stated treatment of clock uncertainty, and
    tests for it, before any measurement taken across those clocks means anything.
    The one consequence already handled is the target age: the elapsed term in
    :meth:`argos.safety.CommandGate.submit` is floored at zero, so a tolerated
    forward stamp cannot make a detection look younger than its producer said.
    """

    def __post_init__(self) -> None:
        """Reject a nonsensical configuration at construction.

        A bad envelope is a programmer error, not a runtime condition: there is no
        safe way to fly with inverted thrust bounds, and refusing every command
        afterwards would report the symptom far from the cause.
        """
        positive = (
            "max_tilt", "max_dyaw", "max_speed", "max_accel", "max_yaw_rate",
            "size_stop", "min_flying_alt",
            "max_command_age", "max_state_age", "max_target_age",
        )
        for name in positive:
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"Envelope.{name} must be finite and positive, got {value!r}")

        # Skew is the one bound whose correct value is zero, so it is checked
        # separately: non-negative and finite rather than strictly positive.
        if not math.isfinite(self.max_clock_skew) or self.max_clock_skew < 0.0:
            raise ValueError(
                f"Envelope.max_clock_skew must be finite and not negative, "
                f"got {self.max_clock_skew!r}"
            )

        for name in ("thrust_min", "thrust_max"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"Envelope.{name} must be finite and within 0..1, got {value!r}")
        if self.thrust_min >= self.thrust_max:
            raise ValueError(
                f"Envelope.thrust_min must be below thrust_max, "
                f"got {self.thrust_min!r} and {self.thrust_max!r}"
            )


def clip(payload: Payload, env: Envelope) -> Payload:
    """``payload`` with every field brought inside ``env``.

    Returns a new payload; the caller's is never modified, because the unclipped
    command is the record of what was asked for.
    """
    if isinstance(payload, AttitudeCmd):
        return AttitudeCmd(
            roll=clamp(payload.roll, -env.max_tilt, env.max_tilt),
            pitch=clamp(payload.pitch, -env.max_tilt, env.max_tilt),
            dyaw=clamp(payload.dyaw, -env.max_dyaw, env.max_dyaw),
            thrust=clamp(payload.thrust, env.thrust_min, env.thrust_max),
        )
    if isinstance(payload, VelocityCmd):
        return VelocityCmd(
            vx=clamp(payload.vx, -env.max_speed, env.max_speed),
            vy=clamp(payload.vy, -env.max_speed, env.max_speed),
            vz=clamp(payload.vz, -env.max_speed, env.max_speed),
            yaw_rate=clamp(payload.yaw_rate, -env.max_yaw_rate, env.max_yaw_rate),
        )
    if isinstance(payload, AccelCmd):
        return AccelCmd(
            ax=clamp(payload.ax, -env.max_accel, env.max_accel),
            ay=clamp(payload.ay, -env.max_accel, env.max_accel),
            az=clamp(payload.az, -env.max_accel, env.max_accel),
            yaw_rate=clamp(payload.yaw_rate, -env.max_yaw_rate, env.max_yaw_rate),
        )
    if isinstance(payload, CtbrCmd):
        # Reachable only if a caller builds a CtbrCmd directly. The gate refuses
        # this space before reaching here; see CommandGate for why.
        raise NotImplementedError(
            "no envelope is defined for body rates: see argos.safety.gate"
        )
    raise TypeError(f"unknown payload type: {type(payload).__name__}")
