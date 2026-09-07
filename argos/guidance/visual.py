"""Visual servoing in attitude, with no position estimate of any kind.

The whole law is a map from *where the target sits in the image* to *how the
aircraft should lean*. It never asks where the aircraft is, which is what lets it
run with no GPS, no rangefinder and no odometry: keeping a target centred is a
question about the image, not about the world.

Three problems have to be solved to make that work, and each one shapes a term.

**There is no velocity feedback, so a proportional term alone oscillates.** Lean
toward the target, gain lateral speed, arrive at the centre at full speed, overshoot,
lean back. The derivative of the *image* error is the missing velocity feedback: the
camera supplies it, not an estimator. It is low-pass filtered because detection
jitters, and a raw derivative of a jittering signal is mostly noise.

**There is no compass worth trusting, so no absolute heading is ever commanded.**
Yaw output is an offset from the heading measured when the command is encoded. A
commanded absolute heading would drift away from the real one; an offset re-anchored
every cycle cannot.

**There is no range sensor, so distance comes from apparent size.** The box grows as
the aircraft closes. That makes every size threshold a geometric calibration rather
than a constant: apparent size is capped by the viewing geometry, and a threshold
set above what the camera can ever produce simply never fires. See
:class:`argos.core.TargetView` for the ceiling and how it is computed.

**What this module does not do.** It produces a desired command and nothing else. It
enforces no bound that matters: :mod:`argos.safety` is what clips and what removes
forward motion near a target, on every path including this one. The limits below are
the law's own authority, chosen so the law is not normally the thing being clipped;
they are not a safety property and must never be read as one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from argos.core import AttitudeCmd, Command, CommandSource, TargetView
from argos.safety.validate import check_clock, check_target

DEG = math.pi / 180.0


class SampleKind(Enum):
    """What the law decided the observation it was handed actually was.

    Reported in the telemetry because these cases are indistinguishable from the
    outside and the difference between them changes the derivative. A view carrying
    ``found=True`` is a claim by the producer, not a fact: the law classifies it
    against its own history before letting it touch anything.
    """

    NONE = "none"          # nothing is designated
    NO_CLOCK = "no_clock"  # the world clock itself cannot be read
    UNUSABLE = "unusable"  # a view was supplied but its numbers cannot be read
    FUTURE = "future"      # a view stamped ahead of the clock: a second time base
    NEW = "new"            # a detection strictly newer than the last one
    REPEATED = "repeated"  # the same detection, handed over again
    LATE = "late"          # a detection older than one already processed
    COASTED = "coasted"    # a lock with no detection on this frame
    NO_TIME = "no_time"    # the clock has not advanced since the last call


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


@dataclass(frozen=True)
class GuidanceGains:
    """One set of gains is one airframe-and-camera profile. Angles are radians."""

    kp_roll: float = 7.0 * DEG    # bank per unit of horizontal image error
    kd_roll: float = 9.0 * DEG    # damping, per unit of image error per second
    kp_yaw: float = 1.5 * DEG     # heading correction per unit of image error
    yaw_deadband: float = 0.08    # below this the heading is left alone: detection noise

    k_pitch: float = 5.5 * DEG    # approach pitch at full remaining distance
    k_brake: float = 6.0 * DEG    # nose-up braking once inside the standoff
    kd_size: float = 10.0 * DEG   # approach damping, per unit of ramp per second

    size_far: float = 0.05        # apparent size at or below which the target is far
    size_near: float = 0.12       # apparent size at which the approach reaches zero
    size_brake: float = 0.05      # width of the braking ramp beyond size_near

    tau_d: float = 0.25           # s, time constant of the derivative low-pass
    max_dt: float = 0.5           # s, longest interval the derivative will trust
    min_dt: float = 1e-3
    """s, shortest interval accepted as a distinct measurement.

    Doing double duty on purpose. It is the shortest gap the derivative will
    divide by, and it is therefore also the resolution at which two detection
    instants count as the same one. Comparing those instants for exact equality
    would be wrong twice over: a producer computing ``t - age`` lands a fraction
    of a microsecond off through ordinary floating-point arithmetic, and an
    interval far below this one carries no usable rate anyway.
    """

    max_tilt: float = 12.0 * DEG
    """The law's own authority in bank and pitch.

    Chosen inside the safety envelope so that clipping by the gate is an exception
    worth looking at rather than the normal state of affairs. It is not a guarantee:
    :class:`argos.safety.Envelope` is what actually bounds anything, and it bounds
    this law exactly as it bounds an operator or a learned policy.
    """

    max_dyaw: float = 5.0 * DEG   # law's own limit on the per-command heading offset

    max_clock_skew: float = 0.0
    """s, how far ahead of the world clock a view may be stamped and still be used.

    Zero, matching :attr:`argos.safety.Envelope.max_clock_skew`, and the two are
    meant to stay equal: a law that trusted a view the safety layer is going to
    refuse would keep updating its memory from observations that never reach the
    vehicle. The law carries its own copy rather than importing the safety
    configuration, because a control law that reads the envelope is one step away
    from reasoning about it.

    A view stamped ahead of the clock is not an unusually fresh measurement. It means
    a second time base leaked in, and its detection instant cannot be placed on this
    one at all.
    """


@dataclass(frozen=True)
class GuidanceTelemetry:
    """What the law was thinking, for logging and for the operator display.

    Values are unrounded. Rounding for display is the display's job; rounding in the
    record throws away the small numbers that a derivative term is made of.
    """

    derr: float = 0.0        # filtered derivative of the horizontal image error, 1/s
    dsize: float = 0.0       # filtered derivative of apparent size, 1/s: closing speed
    approach: float = 0.0    # 0..1, how much distance the law believes is left
    brake: float = 0.0       # 0..1, how far inside the standoff the target is
    dt: float = 0.0          # s, interval the last filter update was taken over
    primed: bool = False     # whether a valid detection has ever been processed
    kind: SampleKind = SampleKind.NONE
    """How the last observation was classified. The one field to read first when a
    derivative looks wrong: it says whether there was a measurement at all."""

    problem: str = ""        # why the view was unusable, when it was


class VisualGuidance:
    """Image error to desired attitude. An object, because the derivative remembers.

    ``step`` stays deterministic for a given internal state, which is what makes the
    law testable at a bench with no camera and no vehicle.
    """

    def __init__(self, gains: GuidanceGains | None = None) -> None:
        self.g = gains or GuidanceGains()
        self.reset()

    def reset(self) -> None:
        """Forget the previous target. Called on every new lock.

        A derivative carried across a re-designation is a derivative between two
        different objects, which is a large meaningless number arriving exactly when
        the aircraft starts moving toward something new.
        """
        self._err_prev = 0.0
        self._size_prev = 0.0
        self._t_seen: float | None = None    # when the target was last DETECTED
        self._t_step: float | None = None    # when this law last ran
        self._derr = 0.0
        self._dsize = 0.0
        self._primed = False
        self.telemetry = GuidanceTelemetry(kind=SampleKind.NONE)

    def _closure(self, size: float) -> tuple[float, float]:
        """``(approach, brake)``, both 0..1, from apparent size.

        ``approach`` is 1 while the target is far and falls to 0 at the standoff
        distance. ``brake`` is positive only beyond it, and is the only term that
        pushes the aircraft backwards.
        """
        g = self.g
        span = max(g.size_near - g.size_far, 1e-6)
        approach = _clamp((g.size_near - size) / span, 0.0, 1.0)
        brake = _clamp((size - g.size_near) / max(g.size_brake, 1e-6), 0.0, 1.0)
        return approach, brake

    @staticmethod
    def detected_at(target: TargetView) -> float | None:
        """When the detection behind this view actually happened, on the world clock.

        Composed from the two documented fields rather than reinterpreting either:
        ``t`` is when the view was computed and ``age`` is how old the detection was
        at that moment, so the detection itself happened at ``t - age``. For a view
        that was detected on its own frame the two coincide, which is the ordinary
        case; they part company exactly when a producer hands over a view it built
        earlier, and that is the case this law has to be able to recognise.
        """
        return None if target.t is None else target.t - target.age

    def step(self, target: TargetView, now: float, engage: bool) -> Command:
        """One cycle. ``now`` is the world clock, and the command is stamped with it.

        ``engage`` has no default and is not optional. It is the operator's decision
        to close on the target, and an approach that happens because an argument was
        forgotten is not a decision. The proximity guard in :mod:`argos.safety` bounds
        the approach; it does not authorise it, and the two must not be confused.

        **The derivative interval is the time between detections, not between calls.**
        The original law divided by the control-loop period, which is only the same
        thing when the detector runs on every cycle. The fielded architecture is the
        opposite -- detect sparsely, track in between -- so with a 10 Hz detector
        inside a 50 Hz loop the old form divided a 100 ms change by 20 ms and reported
        a closing speed five times too large.

        **A view claiming ``found=True`` is a claim, not a fact.** It is classified
        against this law's own history before it is allowed to change anything, so a
        view handed over twice, or one arriving after a newer one, cannot manufacture
        a measurement. Without that, clamping a zero or negative interval up to one
        millisecond turns a sequencing mistake into a plausible-looking rate.

        **An unusable view never reaches the memory.** A single ``NaN`` used to enter
        the filter and stay there: the refusal downstream threw the command away but
        left the derivative permanently ``NaN``, so every later valid observation was
        refused too. Recovery required losing the lock. The view is now checked
        first, and a bad frame costs one cycle rather than the designation.
        """
        g = self.g

        if not target.has:
            self.reset()
            # Level, heading held, altitude held. Not a position hold: with no
            # position estimate the aircraft drifts with the wind while flying this.
            self.telemetry = GuidanceTelemetry(kind=SampleKind.NONE)
            return Command(payload=AttitudeCmd(), source=CommandSource.IDLE, t=now)

        # --- is this view readable at all --------------------------------
        # The same predicate the safety layer applies, used here for a different
        # purpose: not to decide whether to emit, but to keep unreadable numbers out
        # of a filter that has no way to recover from them. Importing the check
        # rather than repeating it keeps one definition of "usable".
        problem = check_clock(now)
        if problem:
            # Nothing below can be computed against an unreadable clock: every
            # comparison against NaN is false, so the classification would fall
            # through to whichever branch happens to be last. The memory, including
            # the time of this call, is left exactly as it was.
            self.telemetry = GuidanceTelemetry(
                derr=self._derr, dsize=self._dsize, primed=self._primed,
                kind=SampleKind.NO_CLOCK, problem=problem,
            )
            return self._command(self._err_prev, self._size_prev, engage, now)

        problem = check_target(target)
        elapsed = 0.0 if self._t_step is None else now - self._t_step

        if problem:
            kind = SampleKind.UNUSABLE
            err, size = self._err_prev, self._size_prev
        elif target.t is not None and target.t > now + g.max_clock_skew:
            # Refused *here*, before the memory is touched, and not only downstream.
            # The safety layer already refuses to emit such a command, but a refusal
            # at the exit does not undo a reference already moved at the entrance: a
            # view stamped in the year 10000 used to be recorded as the newest
            # detection, after which every genuine observation was classified as
            # arriving late and stopped feeding the derivative, with no way back
            # short of losing the designation.
            kind = SampleKind.FUTURE
            problem = (
                f"view is stamped {target.t - now:.3f} s ahead of the world clock, "
                f"tolerance {g.max_clock_skew:.3f} s"
            )
            err, size = self._err_prev, self._size_prev
        else:
            err, size = _clamp(target.error_x, -1.0, 1.0), target.size
            seen_at = self.detected_at(target)
            if not target.found or seen_at is None:
                kind = SampleKind.COASTED
            elif self._t_seen is None:
                kind = SampleKind.NEW
            else:
                # Compared at the law's own resolution rather than exactly. A gap
                # smaller than one it would differentiate over is not a measurement,
                # whichever side of zero it falls on.
                delta = seen_at - self._t_seen
                if delta >= g.min_dt:
                    kind = SampleKind.NEW
                elif delta > -g.min_dt:
                    kind = SampleKind.REPEATED
                else:
                    kind = SampleKind.LATE

        # --- update the memory, or deliberately do not --------------------
        dt = 0.0
        if kind is SampleKind.NEW:
            seen_at = self.detected_at(target)
            if self._primed and self._t_seen is not None:
                dt = _clamp(seen_at - self._t_seen, g.min_dt, g.max_dt)
                alpha = dt / (g.tau_d + dt)
                self._derr += alpha * ((err - self._err_prev) / dt - self._derr)
                self._dsize += alpha * ((size - self._size_prev) / dt - self._dsize)
            self._err_prev, self._size_prev, self._t_seen = err, size, seen_at
            self._primed = True
        elif elapsed > 0.0:
            # Nothing new was measured, but time passed. The stored derivative is
            # decayed toward zero over the gap between *calls*, which reads as this
            # law losing confidence -- what is actually happening -- rather than as
            # the target having stopped. Differentiating a frozen error would give
            # zero now and a spike on reacquisition.
            dt = _clamp(elapsed, g.min_dt, g.max_dt)
            alpha = dt / (g.tau_d + dt)
            self._derr += alpha * (0.0 - self._derr)
            self._dsize += alpha * (0.0 - self._dsize)
        elif kind in (SampleKind.COASTED, SampleKind.REPEATED, SampleKind.LATE):
            # No measurement and no elapsed time. Decaying here would let a caller
            # spin the filter down by calling repeatedly on a stopped clock.
            #
            # Only these three are relabelled. UNUSABLE and FUTURE already say why
            # nothing happened, and overwriting them would report a stopped clock to
            # an operator whose actual problem is a view that cannot be read or that
            # carries a foreign time base.
            kind = SampleKind.NO_TIME

        self._t_step = now

        approach, brake = self._closure(size)
        self.telemetry = GuidanceTelemetry(
            derr=self._derr,
            dsize=self._dsize,
            approach=approach,
            brake=brake,
            dt=dt,
            primed=self._primed,
            kind=kind,
            problem=problem,
        )
        return self._command(err, size, engage, now)

    def _command(self, err: float, size: float, engage: bool, now: float) -> Command:
        """Turn the current filter state and one (error, size) pair into a command.

        Separated so that every exit from :meth:`step`, including the ones taken when
        an input was refused, produces a command the same way. A refusal path that
        built its own command by hand is a second control law nobody reviews.
        """
        g = self.g

        # Lateral: proportional to where the target is, derivative for the damping
        # no estimator is providing.
        roll = _clamp(g.kp_roll * err + g.kd_roll * self._derr, -g.max_tilt, g.max_tilt)

        # Heading: relative, with a deadband so detection noise does not chase itself.
        yaw_err = 0.0 if abs(err) < g.yaw_deadband else err
        dyaw = _clamp(g.kp_yaw * yaw_err, -g.max_dyaw, g.max_dyaw)

        # Approach: nose down while distance remains, nose up once inside the
        # standoff. Nose up is a positive pitch, so advancing is a NEGATIVE one.
        approach, brake = self._closure(size)
        # Normalised by the ramp width so this gain stays in the same units as the
        # others rather than depending on the calibration.
        closure = self._dsize / max(g.size_near - g.size_far, 1e-6)
        pitch = 0.0
        # `primed` gates the approach as well as `engage`: with no valid detection
        # ever processed there is no distance estimate, and an apparent size of zero
        # would otherwise read as "very far away" and command a full advance.
        if engage and self._primed:
            pitch = _clamp(
                -g.k_pitch * approach + g.k_brake * brake + g.kd_size * closure,
                -g.max_tilt,
                g.max_tilt,
            )

        # Thrust stays at the neutral 0.5: the autopilot holds altitude on the
        # barometer, which is what keeps this law free of any vertical estimate.
        return Command(
            payload=AttitudeCmd(roll=roll, pitch=pitch, dyaw=dyaw, thrust=0.5),
            source=CommandSource.TRACK,
            t=now,
        )
