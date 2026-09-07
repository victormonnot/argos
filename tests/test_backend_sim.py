"""The first backend, tested as a vehicle and as a keeper of intents.

Two things are being checked here and they are worth naming apart. The first is
that the model moves the way an aircraft flown by tilting moves: the frame, the
signs, the lag, the terminal speed. The second, and the reason this lot exists, is
the **deposit lifecycle**: when an intent starts acting, how long it keeps acting,
and what makes it stop. Those tests live at the bottom.

Nothing here measures a physical property of any real aircraft. The parameters were
never identified against one, and a distance produced by this integrator is a
property of this integrator.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from argos.backends import GRAVITY, Applied, AttitudeSim, Refusal, Vehicle
from argos.core import (
    NEUTRAL_ATTITUDE,
    AttitudeCmd,
    Command,
    CommandSource,
    CommandSpace,
    VelocityCmd,
    World,
)
from argos.core.truth import Truth

HZ = 50.0
DT = 1.0 / HZ

TICK = 1.0 / 64.0
"""An exactly representable step, for the tests that assert on a deadline.

0.02 s is not a binary fraction, so a sum of them lands a few ulps either side of
half a second and a boundary assertion becomes a question about rounding rather than
about the policy. 1/64 costs nothing and removes that from every test below that
cares which side of an instant something falls on.
"""


def track(payload: AttitudeCmd, t: float) -> Command:
    return Command(payload=payload, source=CommandSource.TRACK, t=t)


def fly(world: AttitudeSim, payload: AttitudeCmd, seconds: float, dt: float = DT) -> None:
    """Emit ``payload`` on every cycle for ``seconds``, the way a control loop would.

    Re-emitting matters: a single deposit expires, so a test that wants sustained
    motion has to keep asking for it, exactly as a real loop does.
    """
    for _ in range(int(round(seconds / dt))):
        world.command(0, track(payload, world.time()))
        world.step(dt)


# --------------------------------------------------------------------------
# The interface
# --------------------------------------------------------------------------


def test_it_is_a_world_and_a_ground_truth() -> None:
    """Both protocols, because in simulation the integrator is the authority.

    The isolation test is what stops a control layer from using the second one.
    """
    world = AttitudeSim()
    assert isinstance(world, World)
    assert isinstance(world, Truth)
    assert world.dim == 3
    assert world.command_space() is CommandSpace.ATTITUDE
    assert world.agents() == (0,)


def test_the_clock_starts_where_it_was_told_and_only_steps_move_it() -> None:
    """Time advances inside step and nowhere else.

    Stated as a test because it is the one property of this backend that a hardware
    backend will not share: there the clock runs whether the loop does or not.
    """
    world = AttitudeSim(t0=100.0)
    assert world.time() == 100.0
    world.command(0, track(AttitudeCmd(), 100.0))
    assert world.time() == 100.0
    world.step(DT)
    assert world.time() == pytest.approx(100.0 + DT)


def test_a_state_handed_out_cannot_be_written_through() -> None:
    """A consumer holding a state must not be able to move the aircraft.

    ``SelfState`` is frozen and its arrays are not, so without this the gate, the
    law or a log could reach into the integrator by accident. This is the producer
    keeping the promise made in :class:`argos.core.SelfState`.
    """
    world = AttitudeSim(pos=[1.0, 2.0, 3.0])
    state = world.observe(0).me

    assert not state.pos.flags.writeable
    assert not state.vel.flags.writeable
    with pytest.raises(ValueError):
        state.pos[0] = 999.0

    # ... and it is a copy, so two reads do not alias each other either.
    first = world.observe(0).me.pos
    fly(world, AttitudeCmd(pitch=-0.1), 0.2)
    assert first[1] == pytest.approx(2.0)
    assert world.observe(0).me.pos[1] != pytest.approx(2.0)


def test_observation_and_truth_agree_while_nothing_is_degraded() -> None:
    """Equal today, and the test says today.

    They are separate names so that inserting a degradation later changes what
    ``observe`` returns without anything having to be found and rewritten. An
    assertion that they are *always* equal would be an assertion that the harness
    can never work.
    """
    world = AttitudeSim(pos=[1.0, 2.0, 3.0], yaw=0.3)
    fly(world, AttitudeCmd(roll=0.05), 0.2)

    seen, true = world.observe(0).me, world.true_state(0)
    assert np.allclose(seen.pos, true.pos)
    assert np.allclose(seen.vel, true.vel)
    assert seen.yaw == true.yaw


def test_this_world_carries_no_neighbours_and_no_target() -> None:
    """One agent, and no sensor model.

    A backend that manufactured a target view would be handing the guidance law
    something it knows from the inside, which is the shape of cheating this
    architecture is built to prevent. Perception produces views.
    """
    observation = AttitudeSim().observe(0)
    assert observation.neighbors == ()
    assert observation.target is None


def test_another_agent_does_not_exist_here() -> None:
    """Single agent, said out loud rather than by silently returning agent zero."""
    world = AttitudeSim()
    with pytest.raises(KeyError):
        world.observe(1)
    with pytest.raises(KeyError):
        world.true_state(1)


# --------------------------------------------------------------------------
# The vehicle
# --------------------------------------------------------------------------


def test_a_neutral_command_holds_position_altitude_and_heading() -> None:
    """The neutral is genuinely neutral in this model, so drift below is real motion."""
    world = AttitudeSim(pos=[0.0, 0.0, 5.0], yaw=0.4)
    fly(world, AttitudeCmd(), 1.0)

    state = world.observe(0).me
    assert np.allclose(state.pos, [0.0, 0.0, 5.0], atol=1e-9)
    assert np.allclose(state.vel, [0.0, 0.0, 0.0], atol=1e-9)
    assert state.yaw == pytest.approx(0.4)


@pytest.mark.parametrize(
    "yaw, axis, name",
    [(0.0, 1, "north"), (math.pi / 2, 0, "east"), (math.pi, 1, "south")],
)
def test_nose_down_advances_along_the_heading(yaw: float, axis: int, name: str) -> None:
    """Forward is a NEGATIVE pitch, and forward means where the nose points.

    This is what pins the frame: heading zero is North, and it grows turning right.
    Two backends picking different zeros would both look right in isolation and
    disagree about which way the aircraft flies.
    """
    world = AttitudeSim(yaw=yaw)
    fly(world, AttitudeCmd(pitch=-0.1), 0.5)

    velocity = world.observe(0).me.vel
    expected = 1.0 if name in ("north", "east") else -1.0
    assert velocity[axis] * expected > 0.1
    assert abs(velocity[1 - axis]) < 1e-9
    assert abs(velocity[2]) < 1e-9


def test_banking_right_accelerates_to_the_right_of_the_heading() -> None:
    """Roll is the other half of the same convention: positive banks right."""
    world = AttitudeSim(yaw=0.0)
    fly(world, AttitudeCmd(roll=0.1), 0.5)

    velocity = world.observe(0).me.vel
    assert velocity[0] > 0.1          # east, which is right of north
    assert abs(velocity[1]) < 1e-9


def test_holding_a_tilt_reaches_a_terminal_speed_rather_than_accelerating_forever() -> None:
    """Drag is what makes a sustained tilt mean a speed instead of a runaway.

    ``g * tan(tilt) / drag``, which is the only closed form this model has and
    therefore the only thing worth asserting numerically about it.
    """
    world = AttitudeSim(vehicle=Vehicle(drag=0.6))
    fly(world, AttitudeCmd(pitch=-0.1), 30.0)

    expected = GRAVITY * math.tan(0.1) / 0.6
    assert world.observe(0).me.vel[1] == pytest.approx(expected, rel=0.02)


def test_without_drag_the_speed_does_not_settle() -> None:
    """The control: remove the term and the terminal speed disappears with it."""
    world = AttitudeSim(vehicle=Vehicle(drag=0.0))
    fly(world, AttitudeCmd(pitch=-0.1), 30.0)

    assert world.observe(0).me.vel[1] > 10.0


def test_attitude_lags_the_command() -> None:
    """No airframe reaches a commanded bank instantly.

    A law tuned against a model that does will be wrong on the first real aircraft,
    so the lag is in the model from the start and is a parameter rather than an
    assumption.
    """
    world = AttitudeSim(vehicle=Vehicle(tau_attitude=0.15))
    world.command(0, track(AttitudeCmd(roll=0.2), 0.0))
    world.step(DT)

    roll, _ = world.attitude
    assert roll == pytest.approx(0.2 * DT / (0.15 + DT), rel=1e-9)
    assert roll < 0.2

    instant = AttitudeSim(vehicle=Vehicle(tau_attitude=0.0))
    instant.command(0, track(AttitudeCmd(roll=0.2), 0.0))
    instant.step(DT)
    assert instant.attitude[0] == pytest.approx(0.2)


def test_thrust_is_read_as_a_climb_rate_around_a_neutral_half() -> None:
    """0.5 holds altitude, per :class:`argos.core.AttitudeCmd`, and the model obeys it.

    The vertical loop is closed by the stand-in autopilot, which is what lets an
    attitude command fly with no vertical estimate at all.
    """
    up = AttitudeSim(vehicle=Vehicle(tau_climb=0.0))
    fly(up, AttitudeCmd(thrust=1.0), 1.0)
    assert up.observe(0).me.vel[2] == pytest.approx(2.0)

    down = AttitudeSim(vehicle=Vehicle(tau_climb=0.0))
    fly(down, AttitudeCmd(thrust=0.0), 1.0)
    assert down.observe(0).me.vel[2] == pytest.approx(-2.0)

    hold = AttitudeSim(vehicle=Vehicle(tau_climb=0.0))
    fly(hold, AttitudeCmd(thrust=0.5), 1.0)
    assert hold.observe(0).me.vel[2] == pytest.approx(0.0)


def test_the_aircraft_does_not_descend_through_the_takeoff_height() -> None:
    """A floor, and only a floor: no contact dynamics, no landing, no ground effect.

    It exists so that a descent reads as an aircraft on the ground rather than one
    at a negative altitude, which every height check downstream would misread.
    """
    world = AttitudeSim(pos=[0.0, 0.0, 0.5])
    fly(world, AttitudeCmd(thrust=0.0), 3.0)

    state = world.observe(0).me
    assert state.pos[2] == 0.0
    assert state.vel[2] >= 0.0


def test_a_heading_offset_is_flown_over_time_rather_than_instantly() -> None:
    """``dyaw`` is an offset, and the heading loop has a rate limit like a real one."""
    world = AttitudeSim(yaw=0.0, vehicle=Vehicle(yaw_rate=1.2))
    world.command(0, track(AttitudeCmd(dyaw=0.5), 0.0))
    world.step(DT)

    assert world.observe(0).me.yaw == pytest.approx(1.2 * DT)


def test_a_heading_offset_takes_the_short_way_round() -> None:
    """Wrapping, so a command near half a turn does not fly the long way."""
    world = AttitudeSim(yaw=3.0, vehicle=Vehicle(yaw_rate=10.0))
    world.command(0, track(AttitudeCmd(dyaw=0.3), 0.0))
    for _ in range(10):
        world.step(DT)

    assert world.observe(0).me.yaw == pytest.approx(-math.pi + (3.3 - math.pi), abs=1e-9)


# --------------------------------------------------------------------------
# What the world refuses to record
# --------------------------------------------------------------------------


def test_a_command_whose_numbers_cannot_be_read_is_declined_at_the_door() -> None:
    """A NaN reaching the integrator is not one bad step, it is all of them.

    Declined rather than raised: a backend sits at the end of a link where a
    malformed message is an ordinary event, and turning one into a stopped control
    loop is the worse failure.
    """
    world = AttitudeSim()
    world.command(0, track(AttitudeCmd(roll=math.nan), 0.0))

    assert world.standing is None
    assert world.refusals[Refusal.INVALID_COMMAND] == 1
    assert "nan" in world.last_refusal.lower()

    world.step(DT)
    assert world.last_applied is Applied.IDLE
    assert np.all(np.isfinite(world.observe(0).me.pos))


def test_a_command_in_another_space_is_declined() -> None:
    """This world speaks attitude. Ignoring a velocity command would fly a zero."""
    world = AttitudeSim()
    world.command(0, Command(payload=VelocityCmd(vx=2.0), source=CommandSource.OPERATOR, t=0.0))

    assert world.standing is None
    assert world.refusals[Refusal.UNSUPPORTED_SPACE] == 1


def test_a_command_for_an_agent_that_does_not_exist_is_declined() -> None:
    world = AttitudeSim()
    world.command(3, track(AttitudeCmd(roll=0.1), 0.0))

    assert world.standing is None
    assert world.refusals[Refusal.UNKNOWN_AGENT] == 1


def test_a_second_deposit_replaces_the_first_and_says_so() -> None:
    """A stream of setpoints, not a queue: the later intent wins.

    Counted, because a loop depositing twice per step is emitting one command that
    never acts, and nothing else would make that visible.
    """
    world = AttitudeSim()
    world.command(0, track(AttitudeCmd(roll=0.1), 0.0))
    world.command(0, track(AttitudeCmd(roll=-0.1), 0.0))

    assert world.overwritten == 1
    assert world.standing.cmd.payload.roll == pytest.approx(-0.1)

    world.step(DT)
    assert world.attitude[0] < 0.0


@pytest.mark.parametrize("dt", [0.0, -DT, math.nan, math.inf, 0.2], ids=str)
def test_a_step_that_cannot_be_integrated_is_refused(dt: float) -> None:
    """Rejected rather than repaired.

    Clamping an oversized step would hide a loop that lost its cadence and report a
    trajectory nobody flew; an explicit integrator does not survive it either way.
    """
    world = AttitudeSim(max_step=0.1)
    with pytest.raises(ValueError):
        world.step(dt)


def test_a_step_at_exactly_the_limit_is_accepted() -> None:
    """The bound is inclusive, so the limit is a usable cadence and not a trap."""
    world = AttitudeSim(max_step=0.1)
    world.step(0.1)
    assert world.time() == pytest.approx(0.1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"drag": -1.0},
        {"tau_attitude": -0.1},
        {"tau_climb": math.nan},
        {"max_climb_rate": 0.0},
        {"yaw_rate": math.inf},
        {"model_tilt_limit": math.pi},
        {"model_tilt_limit": 0.0},
    ],
    ids=str,
)
def test_a_vehicle_that_describes_nothing_is_refused_at_construction(kwargs) -> None:
    """A simulation with nonsense parameters produces numbers that look like results."""
    with pytest.raises(ValueError):
        Vehicle(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"command_lifetime": 0.0},
        {"command_lifetime": math.nan},
        {"max_step": -1.0},
        {"t0": math.inf},
        {"yaw": math.nan},
        {"pos": [0.0, 0.0, math.nan]},
        {"pos": [0.0, 0.0]},
        {"vel": [1.0, 2.0]},
    ],
    ids=str,
)
def test_an_initial_condition_that_cannot_be_flown_is_refused(kwargs) -> None:
    """Checked with the same predicate the safety layer uses, at construction."""
    with pytest.raises(ValueError):
        AttitudeSim(**kwargs)


def test_an_extreme_tilt_gives_a_bounded_wrong_answer() -> None:
    """The model stops describing anything long before ninety degrees.

    ``g * tan(tilt)`` runs to infinity there, and a caller that never passed through
    a safety envelope can hand over any finite angle. The clamp is the difference
    between a wrong number and a number that poisons the integrator.
    """
    world = AttitudeSim(vehicle=Vehicle(model_tilt_limit=math.radians(60)))
    fly(world, AttitudeCmd(pitch=-1.5), 2.0)

    state = world.observe(0).me
    assert np.all(np.isfinite(state.pos))
    assert state.vel[1] <= GRAVITY * math.tan(math.radians(60)) / 0.6 + 1e-6


# --------------------------------------------------------------------------
# The deposit lifecycle: the questions World.command left to a backend
# --------------------------------------------------------------------------


def test_a_deposit_is_fresh_once_and_held_afterwards() -> None:
    """The difference between a new intent and the same one again, made visible.

    Collapsing the two would hide the moment a loop stopped emitting, which is
    exactly the moment worth seeing in a log.
    """
    world = AttitudeSim(command_lifetime=0.5)
    world.command(0, track(AttitudeCmd(roll=0.1), 0.0))

    world.step(DT)
    assert world.last_applied is Applied.FRESH
    world.step(DT)
    assert world.last_applied is Applied.HELD

    world.command(0, track(AttitudeCmd(roll=0.1), world.time()))
    world.step(DT)
    assert world.last_applied is Applied.FRESH


def test_a_deposit_keeps_acting_until_its_lifetime_runs_out() -> None:
    """Silence upstream does not stop the aircraft, it starts a timer.

    This is the answer to "does a refusal cancel a deposit": no. The bound is what
    makes that safe to say, and the bound is this.
    """
    world = AttitudeSim(command_lifetime=0.5, max_step=TICK)
    world.command(0, track(AttitudeCmd(roll=0.2), 0.0))

    for _ in range(32):                       # 0.5 s of silence, at 64 Hz
        world.step(TICK)
    assert world.last_applied is Applied.HELD
    assert world.attitude[0] == pytest.approx(0.2, rel=0.05)

    world.step(TICK)                          # the first step beyond the lifetime
    assert world.last_applied is Applied.EXPIRED
    assert world.expired_at == 0.5


@pytest.mark.parametrize("tick", [1.0 / 64.0, 0.0625, 0.125, 0.25], ids=str)
def test_the_intent_stops_at_its_deadline_whatever_the_cadence(tick: float) -> None:
    """The promise, as a property rather than as one arithmetic example.

    Whatever the step size, a deposit stops acting exactly ``command_lifetime``
    after the world recorded it. Expiring only on step boundaries would have made the
    real bound ``command_lifetime + max_step``, so the announced number would have
    depended on the caller's cadence, which is the one thing a bound must not do.
    """
    world = AttitudeSim(command_lifetime=0.5, max_step=0.25)
    world.command(0, track(AttitudeCmd(roll=0.2), 0.0))

    # Bounded on purpose. An unbounded wait for a condition is not a test: with the
    # deadline check removed it would hang instead of failing, and a hang is the one
    # outcome a failure probe cannot report.
    for _ in range(int(2.0 / tick)):
        world.step(tick)
        if world.last_applied is Applied.EXPIRED:
            break

    assert world.last_applied is Applied.EXPIRED
    assert world.expired_at == 0.5
    assert world.standing is None


def test_the_step_that_crosses_the_deadline_is_split_at_the_deadline() -> None:
    """Not just relabelled: the aircraft really flies two payloads in that step.

    A deadline of 0.15625 s falls strictly inside the third step of 0.0625 s. The
    flight is compared against the same manoeuvre hand-integrated in two pieces --
    the deposit up to the deadline, the neutral afterwards -- so what is asserted is
    the trajectory rather than the label attached to it.
    """
    lifetime, tick = 0.15625, 0.0625
    crossing = AttitudeSim(command_lifetime=lifetime, max_step=tick)
    crossing.command(0, track(AttitudeCmd(roll=0.2), 0.0))
    for _ in range(3):
        crossing.step(tick)
    assert crossing.last_applied is Applied.EXPIRED
    assert crossing.expired_at == lifetime

    reference = AttitudeSim(command_lifetime=10.0, max_step=tick)
    reference.command(0, track(AttitudeCmd(roll=0.2), 0.0))
    reference.step(tick)
    reference.step(tick)
    reference.step(lifetime - 2 * tick)                 # 0.125 -> 0.15625, the deposit
    reference.command(0, track(AttitudeCmd(), reference.time()))
    reference.step(2 * tick - lifetime + tick)          # 0.15625 -> 0.1875, the neutral

    assert crossing.time() == pytest.approx(reference.time())
    assert crossing.attitude == pytest.approx(reference.attitude)
    assert np.allclose(crossing.observe(0).me.pos, reference.observe(0).me.pos)
    assert np.allclose(crossing.observe(0).me.vel, reference.observe(0).me.vel)


def test_a_step_that_swallows_the_whole_lifetime_still_stops_on_time() -> None:
    """One step longer than the lifetime: the intent flies part of it, then stops.

    The degenerate case of the split, and the one where expiring on boundaries would
    have been furthest from the promise.
    """
    world = AttitudeSim(command_lifetime=0.03125, max_step=0.25)
    world.command(0, track(AttitudeCmd(roll=0.2), 0.0))

    world.step(0.25)
    assert world.last_applied is Applied.EXPIRED
    assert world.expired_at == 0.03125
    assert world.attitude[0] > 0.0                      # it did act, briefly
    assert world.last_flown == NEUTRAL_ATTITUDE


def test_an_expired_deposit_is_dropped_and_can_never_act_again() -> None:
    """No stale intent comes back by surprise after a gap.

    Expiry drops the deposit rather than skipping it, so there is nothing left for a
    later step to find. Skipping would leave an aircraft that resumes an old command
    the moment some other condition changes.
    """
    world = AttitudeSim(command_lifetime=0.25, max_step=TICK)
    world.command(0, track(AttitudeCmd(roll=0.3), 0.0))
    for _ in range(16):                       # 16 * 1/64 = 0.25 exactly
        world.step(TICK)
    assert world.last_applied is Applied.HELD

    world.step(TICK)                          # the first step beyond the deadline
    assert world.last_applied is Applied.EXPIRED
    assert world.expired_at == 0.25
    assert world.standing is None

    for _ in range(200):
        world.step(TICK)
    assert world.last_applied is Applied.IDLE
    assert world.applied[Applied.EXPIRED] == 1
    assert world.attitude[0] == pytest.approx(0.0, abs=1e-6)


def test_expiry_returns_the_aircraft_to_neutral_and_not_to_a_standstill() -> None:
    """Falling back to the neutral is not a brake.

    The neutral is level, heading held, altitude held. The aircraft keeps whatever
    velocity it had and loses it only to drag. Reading "the world fell back to
    neutral" as "the aircraft stopped" is the mistake this test exists to prevent.
    """
    world = AttitudeSim(command_lifetime=0.2)
    fly(world, AttitudeCmd(pitch=-0.15), 1.0)
    moving = world.observe(0).me.vel[1]
    assert moving > 0.5

    for _ in range(12):
        world.step(DT)
    assert world.standing is None
    assert world.applied[Applied.EXPIRED] == 1

    just_after = world.observe(0).me.vel[1]
    assert just_after > 0.5 * moving          # still flying, on its own momentum

    for _ in range(500):
        world.step(DT)
    assert world.observe(0).me.vel[1] < 0.01  # and only drag takes it away


def test_a_paused_loop_does_not_age_its_own_deposit() -> None:
    """This backend's clock is driven by ``step``, so no stepping means no ageing.

    True here and false on hardware, where the clock runs whether the loop does or
    not. Code that has to behave the same on both reads ``time()`` and never assumes
    which one it is on.
    """
    world = AttitudeSim(command_lifetime=0.2)
    world.command(0, track(AttitudeCmd(roll=0.2), 0.0))

    assert world.time() == 0.0                # a long pause in the caller, no steps
    world.step(DT)
    assert world.last_applied is Applied.FRESH


def test_a_held_command_holds_a_heading_instead_of_turning_forever() -> None:
    """``dyaw`` is an offset from the heading at encoding, and the anchor is read once.

    Re-reading the current heading on every application would re-anchor the offset
    each step and turn "five degrees right" into "five degrees right per step". The
    two behaviours are far apart and only one of them is what the command means.
    """
    world = AttitudeSim(yaw=0.0, command_lifetime=10.0)
    world.command(0, track(AttitudeCmd(dyaw=math.radians(5)), 0.0))
    for _ in range(50):                       # one second of holding the same intent
        world.step(DT)

    assert world.observe(0).me.yaw == pytest.approx(math.radians(5), abs=1e-9)

    # The contrast: a loop that re-emits is re-anchoring on purpose, and that really
    # is a turn. Same command, different meaning, and the difference is the deposit.
    turning = AttitudeSim(yaw=0.0)
    fly(turning, AttitudeCmd(dyaw=math.radians(5)), 1.0)
    assert turning.observe(0).me.yaw > math.radians(30)


def test_what_the_aircraft_actually_flew_is_recorded_even_when_nobody_asked() -> None:
    """After an expiry the payload flown is this world's neutral, not the last deposit.

    "What was the aircraft doing when the loop went quiet" has no other answer:
    ``standing`` is empty by then, precisely because the deposit was dropped.
    """
    world = AttitudeSim(command_lifetime=0.25, max_step=TICK)
    world.command(0, track(AttitudeCmd(roll=0.2), 0.0))

    world.step(TICK)
    assert world.last_flown.roll == pytest.approx(0.2)

    for _ in range(16):                       # one step is already spent above
        world.step(TICK)
    assert world.last_applied is Applied.EXPIRED
    assert world.last_flown == NEUTRAL_ATTITUDE      # the second of the two flown
    assert world.standing is None


def test_the_lifetime_bounds_the_command_and_not_the_response() -> None:
    """The aircraft is still accelerating after the intent stopped acting.

    Expiry returns the *commanded* attitude to neutral. The actual attitude then
    falls toward it over ``tau_attitude``, and the tilt that remains keeps producing
    acceleration, so speed peaks after the deposit is already gone. Quoting the
    lifetime as the moment the aircraft stops obeying is wrong by about a time
    constant, and by more than that on a real airframe.
    """
    world = AttitudeSim(
        command_lifetime=0.25, max_step=TICK, vehicle=Vehicle(tau_attitude=0.15)
    )
    world.command(0, track(AttitudeCmd(pitch=-0.12), 0.0))
    for _ in range(17):
        world.step(TICK)

    assert world.last_applied is Applied.EXPIRED
    assert world.last_flown == NEUTRAL_ATTITUDE          # commanded neutral already
    at_expiry = world.observe(0).me.vel[1]
    assert world.attitude[1] < -0.05                     # but still pitched down

    for _ in range(10):
        world.step(TICK)
    assert world.observe(0).me.vel[1] > at_expiry        # and still speeding up
