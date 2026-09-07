"""What the guidance law computes, and the probes that make each term fail.

Every damping term is tested by switching it off and showing the failure it exists
to prevent. A term whose gain can be set to zero with no visible consequence is not
doing anything, and a test that only shows the law working would not tell them apart.

**Scope.** These are tests about the command the law produces from an image error.
Nothing here simulates dynamics, so nothing here establishes an overshoot, a
stopping distance or a separation. The law also enforces no bound that matters:
:mod:`argos.safety` does that, and it is tested separately.
"""
from __future__ import annotations

import math
from dataclasses import replace as dataclass_replace

import pytest

from argos.core import AttitudeCmd, CommandSource, TargetView
from argos.guidance import (DEG, GuidanceGains, SampleKind, VisualGuidance,
                            operator_command)

T0 = 100.0


def seen(t: float, error_x: float = 0.0, size: float = 0.05) -> TargetView:
    """A target detected on this frame, stamped on the world clock."""
    return TargetView(has=True, found=True, error_x=error_x, size=size, t=t)


def coasted(t: float, error_x: float = 0.0, size: float = 0.05, age: float = 0.1) -> TargetView:
    """A locked target not detected on this frame: the values are frozen."""
    return TargetView(has=True, found=False, error_x=error_x, size=size, age=age, t=t)


# --------------------------------------------------------------------------
# No target
# --------------------------------------------------------------------------


def test_without_a_target_the_law_asks_for_nothing() -> None:
    """Level, heading held, altitude held, credited to nobody.

    Not a position hold: with no position estimate the aircraft drifts with the wind
    while flying exactly this command.
    """
    law = VisualGuidance()
    cmd = law.step(TargetView(), now=T0, engage=True)

    assert cmd.payload == AttitudeCmd()
    assert cmd.source is CommandSource.IDLE
    assert cmd.t == T0


def test_losing_the_target_forgets_the_derivative() -> None:
    """A derivative carried across a re-designation is taken between two objects.

    It would arrive as a large meaningless number at the exact moment the aircraft
    starts moving toward something new.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.0), now=T0, engage=True)
    law.step(seen(T0 + 0.1, error_x=0.5), now=T0 + 0.1, engage=True)
    assert law.telemetry.derr != 0.0

    law.step(TargetView(), now=T0 + 0.2, engage=True)
    assert law.telemetry.derr == 0.0
    assert law.telemetry.primed is False


def test_the_command_is_stamped_so_the_safety_gate_will_accept_it() -> None:
    """The gate refuses an unstamped command, so the law has to stamp its own."""
    law = VisualGuidance()
    assert law.step(seen(T0, error_x=0.3), now=T0, engage=True).t == T0
    assert law.step(TargetView(), now=T0 + 1.0, engage=True).t == T0 + 1.0


# --------------------------------------------------------------------------
# The lateral axis
# --------------------------------------------------------------------------


@pytest.mark.parametrize("error_x", [0.8, 0.3, -0.3, -0.8])
def test_roll_points_at_the_target_and_is_symmetric(error_x) -> None:
    """Target right of centre means bank right, and the mirror image is the mirror."""
    law = VisualGuidance()
    cmd = law.step(seen(T0, error_x=error_x), now=T0, engage=True)
    mirrored = VisualGuidance().step(seen(T0, error_x=-error_x), now=T0, engage=True)

    assert math.copysign(1.0, cmd.payload.roll) == math.copysign(1.0, error_x)
    assert cmd.payload.roll == pytest.approx(-mirrored.payload.roll)


def test_the_derivative_damps_a_target_returning_to_centre() -> None:
    """Without velocity feedback a proportional term overshoots; this supplies it.

    The target is off to the right and coming back fast. The damping term should pull
    the bank *below* what the position term alone would ask for, because the aircraft
    is already going to arrive.
    """
    gains = GuidanceGains()
    damped = VisualGuidance(gains)
    undamped = VisualGuidance(GuidanceGains(kd_roll=0.0))

    for law in (damped, undamped):
        law.step(seen(T0, error_x=0.8), now=T0, engage=True)

    with_d = damped.step(seen(T0 + 0.1, error_x=0.3), now=T0 + 0.1, engage=True)
    without_d = undamped.step(seen(T0 + 0.1, error_x=0.3), now=T0 + 0.1, engage=True)

    assert with_d.payload.roll < without_d.payload.roll
    assert damped.telemetry.derr < 0.0


def test_with_the_damping_gain_at_zero_the_law_is_purely_proportional() -> None:
    """The switch that proves the term above is the one doing the work."""
    law = VisualGuidance(GuidanceGains(kd_roll=0.0))
    law.step(seen(T0, error_x=0.8), now=T0, engage=True)
    cmd = law.step(seen(T0 + 0.1, error_x=0.3), now=T0 + 0.1, engage=True)

    assert cmd.payload.roll == pytest.approx(GuidanceGains().kp_roll * 0.3)


# --------------------------------------------------------------------------
# The derivative interval: detections, not control cycles
# --------------------------------------------------------------------------


def test_the_derivative_uses_the_detection_interval_not_the_call_interval() -> None:
    """The defect found while porting, and the reason it matters.

    The fielded architecture detects sparsely and tracks in between. The original law
    divided by the control-loop period, so a 100 ms change seen inside a 20 ms loop
    was reported as a closing speed five times too large and fed straight into the
    damping terms. Here the law is called five times per detection and must produce
    the same derivative as one call per detection.
    """
    sparse = VisualGuidance()
    dense = VisualGuidance()

    sparse.step(seen(T0, error_x=0.0), now=T0, engage=True)
    sparse.step(seen(T0 + 0.1, error_x=0.5), now=T0 + 0.1, engage=True)

    dense.step(seen(T0, error_x=0.0), now=T0, engage=True)
    for i in range(1, 5):                       # four coasted cycles at 50 Hz
        dense.step(coasted(T0, error_x=0.0), now=T0 + 0.02 * i, engage=True)
    dense.step(seen(T0 + 0.1, error_x=0.5), now=T0 + 0.1, engage=True)

    assert dense.telemetry.dt == pytest.approx(0.1)
    assert dense.telemetry.derr == pytest.approx(sparse.telemetry.derr, rel=0.35)
    assert dense.telemetry.derr < 5.0 * 0.5 / 0.02   # nowhere near the loop-rate value


def test_an_absurd_interval_cannot_blow_up_the_derivative() -> None:
    """A loop that stalls must not turn a stale pair of samples into a huge rate."""
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.0), now=T0, engage=True)
    law.step(seen(T0 + 3600.0, error_x=1.0), now=T0 + 3600.0, engage=True)

    assert law.telemetry.dt <= GuidanceGains().max_dt
    assert abs(law.telemetry.derr) < 10.0
    assert abs(law.step(seen(T0 + 3600.1, error_x=1.0), now=T0 + 3600.1, engage=True).payload.roll) <= GuidanceGains().max_tilt


def test_coasting_decays_the_derivative_instead_of_spiking_on_reacquisition() -> None:
    """A frozen image error differentiates to zero now and to a spike later.

    Decaying the stored value reads as the law losing confidence, which is what is
    actually happening, rather than as the target having stopped.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.0), now=T0, engage=True)
    law.step(seen(T0 + 0.1, error_x=0.6), now=T0 + 0.1, engage=True)
    after_detection = law.telemetry.derr
    assert after_detection > 0.0

    previous = after_detection
    for i in range(1, 6):
        law.step(coasted(T0 + 0.1, error_x=0.6), now=T0 + 0.1 + 0.05 * i, engage=True)
        assert 0.0 <= law.telemetry.derr < previous
        previous = law.telemetry.derr


# --------------------------------------------------------------------------
# Heading
# --------------------------------------------------------------------------


def test_heading_has_a_deadband_and_a_limit() -> None:
    """Detection noise must not make the aircraft chase its own jitter.

    The error values are literals rather than multiples of ``yaw_deadband``. Scaling
    the input by the very constant under test makes the assertion true for any value
    of it, including zero, which is a test that cannot fail.
    """
    gains = GuidanceGains(yaw_deadband=0.2)
    law = VisualGuidance(gains)

    assert law.step(seen(T0, error_x=0.1), now=T0, engage=True).payload.dyaw == 0.0
    law.reset()
    assert law.step(seen(T0, error_x=0.5), now=T0, engage=True).payload.dyaw > 0.0
    law.reset()
    assert abs(law.step(seen(T0, error_x=1.0), now=T0, engage=True).payload.dyaw) <= gains.max_dyaw


def test_the_law_never_produces_an_absolute_heading() -> None:
    """The payload carries an offset. There is no field here that could hold a heading."""
    cmd = VisualGuidance().step(seen(T0, error_x=0.5), now=T0, engage=True)
    assert hasattr(cmd.payload, "dyaw")
    assert not hasattr(cmd.payload, "yaw")


# --------------------------------------------------------------------------
# The approach axis
# --------------------------------------------------------------------------


def test_far_advances_near_stops_closer_backs_off() -> None:
    """The three regimes of the standoff controller, read from apparent size alone."""
    gains = GuidanceGains()
    law = VisualGuidance(gains)

    far = law.step(seen(T0, size=gains.size_far * 0.5), now=T0, engage=True)
    assert far.payload.pitch < 0.0                       # nose down: advancing

    law.reset()
    at_standoff = law.step(seen(T0, size=gains.size_near), now=T0, engage=True)
    assert at_standoff.payload.pitch == pytest.approx(0.0, abs=1e-9)

    law.reset()
    too_close = law.step(seen(T0, size=gains.size_near + gains.size_brake), now=T0, engage=True)
    assert too_close.payload.pitch > 0.0                 # nose up: backing away


def test_a_target_charging_the_aircraft_produces_braking() -> None:
    """The prototype of "it does not touch me": closing fast must push the nose up.

    This shows the command reverses. It does not show a distance is kept: no dynamics
    are simulated here and the aircraft does not move.
    """
    law = VisualGuidance()
    sizes = [0.04, 0.07, 0.11, 0.16, 0.22]
    pitches = []
    for i, size in enumerate(sizes):
        pitches.append(law.step(seen(T0 + 0.1 * i, size=size), now=T0 + 0.1 * i, engage=True).payload.pitch)

    assert pitches[0] < 0.0
    assert pitches[-1] > 0.0
    assert pitches == sorted(pitches)                    # monotonic, no reversal


def test_without_the_approach_damping_the_law_is_a_pure_proportional_term() -> None:
    """The switch for the closing-speed term."""
    gains = GuidanceGains(kd_size=0.0)
    law = VisualGuidance(gains)
    law.step(seen(T0, size=0.04), now=T0, engage=True)
    cmd = law.step(seen(T0 + 0.1, size=0.08), now=T0 + 0.1, engage=True)

    approach, brake = law._closure(0.08)
    assert cmd.payload.pitch == pytest.approx(-gains.k_pitch * approach + gains.k_brake * brake)


def test_the_closing_speed_term_brakes_a_fast_approach_earlier() -> None:
    """Same apparent size, different closing speed, and the faster one brakes sooner."""
    fast = VisualGuidance()
    slow = VisualGuidance()

    fast.step(seen(T0, size=0.02), now=T0, engage=True)
    fast_cmd = fast.step(seen(T0 + 0.1, size=0.10), now=T0 + 0.1, engage=True)

    slow.step(seen(T0, size=0.09), now=T0, engage=True)
    slow_cmd = slow.step(seen(T0 + 0.1, size=0.10), now=T0 + 0.1, engage=True)

    assert fast.telemetry.dsize > slow.telemetry.dsize
    assert fast_cmd.payload.pitch > slow_cmd.payload.pitch


def test_without_engage_nothing_advances() -> None:
    """The approach axis is off until the operator asks for it; the rest still tracks."""
    law = VisualGuidance()
    cmd = law.step(seen(T0, error_x=0.5, size=0.02), now=T0, engage=False)

    assert cmd.payload.pitch == 0.0
    assert cmd.payload.roll != 0.0


# --------------------------------------------------------------------------
# The law stays inside its own authority
# --------------------------------------------------------------------------


def test_absurd_targets_never_exceed_the_laws_own_limits() -> None:
    """Its own limits, which are not a safety property: the gate is.

    Included because a law that quietly produces a 90-degree bank would be relying on
    the gate to hide it, and the gate's interventions are supposed to be rare enough
    to be worth reading.
    """
    gains = GuidanceGains()
    law = VisualGuidance(gains)

    for i in range(200):
        view = seen(
            T0 + 0.01 * i,
            error_x=[-50.0, 50.0, 0.0, 3.0][i % 4],
            size=[0.0, 5.0, 0.12, 1.0][i % 4],
        )
        payload = law.step(view, now=T0 + 0.01 * i, engage=True).payload
        assert abs(payload.roll) <= gains.max_tilt
        assert abs(payload.pitch) <= gains.max_tilt
        assert abs(payload.dyaw) <= gains.max_dyaw
        assert payload.thrust == 0.5


# --------------------------------------------------------------------------
# The operator speaks the same language
# --------------------------------------------------------------------------


def test_the_operator_produces_the_same_type_as_the_law() -> None:
    """Which is what lets the safety layer treat a human like anything else."""
    from_law = VisualGuidance().step(seen(T0, error_x=0.3), now=T0, engage=True)
    from_human = operator_command(T0, forward=0.5, right=0.2)

    assert type(from_human.payload) is type(from_law.payload)
    assert from_human.source is CommandSource.OPERATOR
    assert from_law.source is CommandSource.TRACK
    assert from_human.t == T0


def test_operator_forward_is_nose_down_like_the_law() -> None:
    """A sign disagreement here would send a human the opposite way from the autonomy."""
    assert operator_command(T0, forward=1.0).payload.pitch < 0.0
    assert operator_command(T0, right=1.0).payload.roll > 0.0
    assert operator_command(T0, yaw=1.0).payload.dyaw > 0.0


def test_a_centred_operator_stick_holds_altitude() -> None:
    """Neutral thrust is 0.5; a stick at rest must not command a climb or a descent."""
    assert operator_command(T0).payload.thrust == pytest.approx(0.5)
    assert operator_command(T0, up=1.0).payload.thrust == pytest.approx(1.0)
    assert operator_command(T0, up=-1.0).payload.thrust == pytest.approx(0.0)


@pytest.mark.parametrize("axis", ["forward", "right", "up", "yaw"])
def test_operator_input_beyond_the_stick_range_is_saturated(axis) -> None:
    """A miscalibrated stick or a stale packet must not command more than full deflection."""
    saturated = operator_command(T0, **{axis: 99.0}).payload
    full = operator_command(T0, **{axis: 1.0}).payload
    assert saturated == full


# --------------------------------------------------------------------------
# G1: an unusable view must not reach the memory, and the law must recover
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_field", ["error_x", "size"], ids=["error_x", "size"]
)
def test_one_bad_frame_costs_a_cycle_not_the_lock(bad_field) -> None:
    """A single NaN used to poison the filter for the rest of the designation.

    The command was thrown away downstream, but the derivative stayed NaN, so every
    later valid observation produced a NaN command and was refused too. Recovery
    needed a lost lock. The chain here is valid, invalid, then four valid frames, and
    the designation is held throughout.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.1, size=0.05), now=T0, engage=True)
    law.step(seen(T0 + 0.1, error_x=0.2, size=0.06), now=T0 + 0.1, engage=True)
    healthy = law.telemetry.derr
    assert math.isfinite(healthy)

    bad = TargetView(has=True, found=True, error_x=0.3, size=0.07, t=T0 + 0.2)
    bad = dataclass_replace(bad, **{bad_field: math.nan})
    cmd = law.step(bad, now=T0 + 0.2, engage=True)

    assert law.telemetry.kind is SampleKind.UNUSABLE
    assert bad_field in law.telemetry.problem
    assert math.isfinite(law.telemetry.derr)
    assert math.isfinite(cmd.payload.roll) and math.isfinite(cmd.payload.pitch)

    for i in range(4):
        good = seen(T0 + 0.3 + 0.1 * i, error_x=0.3, size=0.07)
        cmd = law.step(good, now=T0 + 0.3 + 0.1 * i, engage=True)
        assert law.telemetry.kind is SampleKind.NEW
        assert math.isfinite(law.telemetry.derr)
        assert math.isfinite(cmd.payload.roll)


def test_a_non_numeric_view_is_refused_rather_than_raised() -> None:
    """``error_x="bad"`` used to raise a TypeError inside the law's own arithmetic.

    It is constructible in TargetView, so it is a value the law can be handed, and a
    value gets a refusal rather than an exception in the control loop.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.2), now=T0, engage=True)

    cmd = law.step(
        TargetView(has=True, found=True, error_x="bad", size=0.05, t=T0 + 0.1),
        now=T0 + 0.1,
        engage=True,
    )

    assert law.telemetry.kind is SampleKind.UNUSABLE
    assert "not a real number" in law.telemetry.problem
    assert math.isfinite(cmd.payload.roll)


def test_an_unusable_view_falls_back_on_the_last_valid_measurement() -> None:
    """The lock is kept, so the law coasts rather than levelling out with a jolt."""
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.5, size=0.05), now=T0, engage=True)
    good = law.step(seen(T0 + 0.1, error_x=0.5, size=0.05), now=T0 + 0.1, engage=True)

    bad = law.step(
        TargetView(has=True, found=True, error_x=math.nan, size=0.05, t=T0 + 0.2),
        now=T0 + 0.2,
        engage=True,
    )

    assert bad.payload.roll == pytest.approx(good.payload.roll, rel=0.35)
    assert bad.payload.roll != 0.0


def test_a_first_frame_that_is_unusable_does_not_advance() -> None:
    """With no valid detection ever processed there is no distance estimate.

    An apparent size of zero would otherwise read as "very far away" and command a
    full advance on the strength of a frame that could not be read.
    """
    law = VisualGuidance()
    cmd = law.step(
        TargetView(has=True, found=True, error_x=math.nan, size=math.nan, t=T0),
        now=T0,
        engage=True,
    )

    assert law.telemetry.primed is False
    assert cmd.payload.pitch == 0.0
    assert cmd.payload.roll == 0.0


# --------------------------------------------------------------------------
# G2: a view claiming to be a detection is a claim, not a fact
# --------------------------------------------------------------------------


def test_the_same_view_handed_over_twice_is_not_a_second_measurement() -> None:
    """A loop faster than the detector re-reads the same view without a camera fault.

    Clamping the zero interval up to a millisecond turned that into a plausible rate
    and let the filter drift once per cycle.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.0), now=T0, engage=True)
    view = seen(T0 + 0.1, error_x=0.2)
    law.step(view, now=T0 + 0.1, engage=True)
    after_first = law.telemetry.derr

    law.step(view, now=T0 + 0.12, engage=True)
    assert law.telemetry.kind is SampleKind.REPEATED
    assert law.telemetry.derr < after_first          # decayed as a coast, not re-measured
    assert law.telemetry.dt == pytest.approx(0.02)   # the call gap, not a fabricated 1 ms


def test_repeating_a_view_without_advancing_the_clock_changes_nothing() -> None:
    """No measurement and no elapsed time means no reason for anything to move.

    Otherwise a caller could spin the filter down simply by calling in a tight loop.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.0), now=T0, engage=True)
    view = seen(T0 + 0.1, error_x=0.2)
    law.step(view, now=T0 + 0.1, engage=True)
    frozen = law.telemetry.derr

    for _ in range(5):
        law.step(view, now=T0 + 0.1, engage=True)
        assert law.telemetry.kind is SampleKind.NO_TIME
        assert law.telemetry.derr == frozen


def test_a_late_view_does_not_rewind_the_reference() -> None:
    """A measurement arriving after a newer one must not move the history backwards.

    It used to: the reference was rewound to the older instant, so the *next*
    genuine detection reported a 150 ms interval where 100 ms had passed.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.0), now=T0, engage=True)
    law.step(seen(T0 + 0.1, error_x=0.2), now=T0 + 0.1, engage=True)

    law.step(seen(T0 + 0.05, error_x=0.9), now=T0 + 0.12, engage=True)
    assert law.telemetry.kind is SampleKind.LATE

    law.step(seen(T0 + 0.2, error_x=0.4), now=T0 + 0.2, engage=True)
    assert law.telemetry.kind is SampleKind.NEW
    assert law.telemetry.dt == pytest.approx(0.1)    # not 0.15


def test_the_detection_instant_is_the_view_time_minus_its_age() -> None:
    """The two documented fields composed, rather than either one reinterpreted.

    ``t`` is when the view was computed and ``age`` is how old the detection was at
    that moment, so the detection happened at ``t - age``. They coincide for a view
    detected on its own frame, which is the ordinary case.
    """
    assert VisualGuidance.detected_at(TargetView(t=100.0, age=0.0)) == 100.0
    assert VisualGuidance.detected_at(TargetView(t=100.0, age=0.3)) == pytest.approx(99.7)
    assert VisualGuidance.detected_at(TargetView(t=None)) is None


def test_a_view_rebuilt_from_an_older_detection_is_not_a_new_measurement() -> None:
    """A producer that refreshes ``t`` while ``age`` grows has not measured anything.

    Both views below describe a detection at T0 + 0.1; only the wrapper is newer.
    The two instants are compared at the law's own resolution rather than exactly:
    ``100.15 - 0.05`` is ``100.10000000000001``, so exact equality would have called
    this a new measurement taken a fraction of a microsecond later.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.0), now=T0, engage=True)
    law.step(seen(T0 + 0.1, error_x=0.2), now=T0 + 0.1, engage=True)

    rebuilt = TargetView(has=True, found=True, error_x=0.2, size=0.05,
                         t=T0 + 0.15, age=0.05)
    law.step(rebuilt, now=T0 + 0.15, engage=True)

    assert law.telemetry.kind is SampleKind.REPEATED


# --------------------------------------------------------------------------
# G3: closing on the target is an operator decision, never a default
# --------------------------------------------------------------------------


def test_engage_has_no_default_and_must_be_stated() -> None:
    """An approach that happens because an argument was forgotten is not a decision.

    The safety layer bounds an approach; it does not authorise one, and the two must
    not be confused.
    """
    law = VisualGuidance()
    with pytest.raises(TypeError, match="engage"):
        law.step(seen(T0, size=0.02), now=T0)          # type: ignore[call-arg]


@pytest.mark.parametrize("engage, advances", [(True, True), (False, False)])
def test_both_explicit_choices_do_what_they_say(engage, advances) -> None:
    law = VisualGuidance()
    cmd = law.step(seen(T0, error_x=0.5, size=0.02), now=T0, engage=engage)

    assert (cmd.payload.pitch < 0.0) is advances
    assert cmd.payload.roll != 0.0                     # tracking continues either way


# --------------------------------------------------------------------------
# G1, temporal half: a view refused for its clock must not move the reference
# --------------------------------------------------------------------------


def test_a_future_view_never_becomes_the_newest_detection() -> None:
    """Refusing a command at the exit does not undo a reference moved at the entrance.

    A view stamped in the year 10000 used to be recorded as the newest detection. The
    gate refused to emit it, but every genuine observation afterwards was then older
    than the stored reference, so all of them were classified as arriving late and
    none fed the derivative again. Only losing the designation recovered.

    The designation is held throughout here: valid, future, then three valid.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.2), now=T0, engage=True)
    assert law._t_seen == pytest.approx(T0)

    future = TargetView(has=True, found=True, error_x=0.9, size=0.30, t=T0 + 9900.0)
    law.step(future, now=T0 + 0.1, engage=True)

    assert law.telemetry.kind is SampleKind.FUTURE
    assert "ahead of the world clock" in law.telemetry.problem
    assert law._t_seen == pytest.approx(T0)          # reference untouched

    for i in range(2, 5):
        law.step(seen(T0 + 0.1 * i, error_x=0.2), now=T0 + 0.1 * i, engage=True)
        assert law.telemetry.kind is SampleKind.NEW
        assert law._t_seen == pytest.approx(T0 + 0.1 * i)


def test_a_future_view_leaves_no_derivative_behind() -> None:
    """Its numbers are finite, so nothing else would have stopped them entering.

    The control alongside shows the same sequence without the future view producing
    the same derivative, which is what "no trace" has to mean.
    """
    contaminated = VisualGuidance()
    clean = VisualGuidance()

    contaminated.step(seen(T0, error_x=0.0), now=T0, engage=True)
    contaminated.step(
        TargetView(has=True, found=True, error_x=0.9, size=0.30, t=T0 + 9900.0),
        now=T0 + 0.1,
        engage=True,
    )
    contaminated.step(seen(T0 + 0.2, error_x=0.4), now=T0 + 0.2, engage=True)

    clean.step(seen(T0, error_x=0.0), now=T0, engage=True)
    clean.step(coasted(T0, error_x=0.0), now=T0 + 0.1, engage=True)
    clean.step(seen(T0 + 0.2, error_x=0.4), now=T0 + 0.2, engage=True)

    assert contaminated.telemetry.derr == pytest.approx(clean.telemetry.derr)
    assert contaminated._t_seen == clean._t_seen


def test_a_first_view_from_the_future_primes_nothing() -> None:
    """With no history to protect, the failure is that it would create one.

    Recorded as the origin of the timeline, it would make every later observation
    late for the rest of the designation.
    """
    law = VisualGuidance()
    cmd = law.step(
        TargetView(has=True, found=True, error_x=0.5, size=0.02, t=T0 + 9900.0),
        now=T0,
        engage=True,
    )

    assert law.telemetry.kind is SampleKind.FUTURE
    assert law._t_seen is None
    assert law.telemetry.primed is False
    assert cmd.payload.pitch == 0.0          # no distance estimate, so no approach
    assert cmd.payload.roll == 0.0

    law.step(seen(T0 + 0.1, error_x=0.3), now=T0 + 0.1, engage=True)
    assert law.telemetry.kind is SampleKind.NEW
    assert law._t_seen == pytest.approx(T0 + 0.1)


def test_a_future_view_is_not_reported_as_a_stopped_clock() -> None:
    """The two are different problems and an operator has to be able to tell them apart.

    Both leave the filter alone, so the classification is the only thing that says
    which happened.
    """
    law = VisualGuidance()
    law.step(
        TargetView(has=True, found=True, error_x=0.5, size=0.02, t=T0 + 9900.0),
        now=T0,
        engage=True,
    )
    assert law.telemetry.kind is SampleKind.FUTURE

    law2 = VisualGuidance()
    law2.step(seen(T0, error_x=0.1), now=T0, engage=True)
    law2.step(seen(T0 + 0.1, error_x=0.2), now=T0 + 0.1, engage=True)
    law2.step(seen(T0 + 0.1, error_x=0.2), now=T0 + 0.1, engage=True)
    assert law2.telemetry.kind is SampleKind.NO_TIME


def test_an_unreadable_clock_freezes_the_law_without_corrupting_it() -> None:
    """Every comparison against NaN is false, so classification would fall through.

    The call time is not recorded either: storing NaN would make the next call's
    elapsed interval NaN for the rest of the run.
    """
    law = VisualGuidance()
    law.step(seen(T0, error_x=0.3), now=T0, engage=True)
    law.step(seen(T0 + 0.1, error_x=0.5), now=T0 + 0.1, engage=True)
    healthy_derr, healthy_seen, healthy_step = law._derr, law._t_seen, law._t_step

    cmd = law.step(seen(T0 + 0.2, error_x=0.7), now=math.nan, engage=True)

    assert law.telemetry.kind is SampleKind.NO_CLOCK
    assert "world clock" in law.telemetry.problem
    assert (law._derr, law._t_seen, law._t_step) == (healthy_derr, healthy_seen, healthy_step)
    assert math.isfinite(cmd.payload.roll)

    law.step(seen(T0 + 0.3, error_x=0.7), now=T0 + 0.3, engage=True)
    assert law.telemetry.kind is SampleKind.NEW
    assert math.isfinite(law._derr)


def test_every_layer_agrees_on_the_clock_tolerance() -> None:
    """Three copies of one policy, pinned together.

    Each layer keeps its own value rather than reading another layer's configuration,
    so nothing but this test stops them drifting. A law more tolerant than the gate
    would keep learning from views that never reach the vehicle; a law less tolerant
    would silently stop tracking inside the tolerance the gate allows. The tracker
    joins them because it makes the same judgement one step earlier: evidence stamped
    ahead of the clock is a second time base wherever it is noticed.
    """
    from argos.perception import TrackPolicy
    from argos.safety import Envelope

    assert (
        GuidanceGains().max_clock_skew
        == Envelope().max_clock_skew
        == TrackPolicy().max_clock_skew
        == 0.0
    )
