"""What the gate does to commands, and the probes that make it fail.

Every test here is written to break the gate rather than to exercise it. A check
that has only ever been shown working has not been shown to check anything: what
matters is that it still holds when the caller is wrong, adversarial, or in a state
nobody anticipated.

**Scope, stated once so no test name has to carry it.** These are tests about
command transformation and admission in a world that never moves. They establish
what the gate deposits and what it refuses. They establish nothing about distance,
momentum, contact, or any other physical outcome: no dynamics are simulated here
and no independent reference measures anything. Those questions belong to a moving
simulation and are open.
"""
from __future__ import annotations

import math
import random

import numpy as np
import pytest

from argos.core import (
    AccelCmd,
    AttitudeCmd,
    Command,
    CommandSource,
    CommandSpace,
    CtbrCmd,
    NEUTRAL_ATTITUDE,
    SelfState,
    TargetView,
    VelocityCmd,
)
from argos.safety import CommandGate, Envelope, Intervention, is_flying


class RecordingWorld:
    """A world with a clock you set by hand, that records what reaches it.

    The clock is the point: the gate compares command, state and target timestamps
    against :meth:`time`, and a test that used the wall clock could not put an input
    a known number of seconds into the past without sleeping.

    Everything the gate is supposed to stop must be absent from ``received``.
    Asserting on the return value alone would not prove that: a gate could report a
    refusal and still have deposited the command.
    """

    def __init__(self, space: CommandSpace = CommandSpace.ATTITUDE, dim: int = 3) -> None:
        self._space = space
        self._dim = dim
        self.now = 100.0
        self.received: list[Command] = []

    @property
    def dim(self) -> int:
        return self._dim

    def command_space(self) -> CommandSpace:
        return self._space

    def agents(self):
        return (0,)

    def time(self) -> float:
        return self.now

    def observe(self, agent):
        raise NotImplementedError("not needed by these tests")

    def command(self, agent, cmd: Command) -> None:
        self.received.append(cmd)

    def step(self, dt: float) -> None:
        self.now += dt


def state_at(world: RecordingWorld, alt: float = 5.0, **kw) -> SelfState:
    """A valid, current, airborne state on the world's clock."""
    return SelfState(
        t=world.now,
        pos=np.array([0.0, 0.0, alt]),
        vel=np.zeros(3),
        armed=True,
        **kw,
    )


def cmd_at(world: RecordingWorld, payload, source=CommandSource.TRACK) -> Command:
    return Command(payload=payload, source=source, t=world.now)


def target_at(world: RecordingWorld, **kw) -> TargetView:
    kw.setdefault("has", True)
    kw.setdefault("found", True)
    return TargetView(t=world.now, **kw)


# --------------------------------------------------------------------------
# The envelope holds against an adversary
# --------------------------------------------------------------------------


def test_adversarial_commands_never_leave_the_envelope() -> None:
    """Two thousand absurd commands, and not one escapes the bounds.

    The probe for this module's central claim: the bounds hold even when the thing
    producing commands is wrong. A control law that has diverged, a policy that was
    never trained, an operator input read from a disconnected stick all look like
    this.
    """
    env = Envelope()
    world = RecordingWorld()
    gate = CommandGate(world, envelope=env)
    rng = random.Random(20260904)

    for _ in range(2000):
        gate.submit(
            cmd_at(
                world,
                AttitudeCmd(
                    roll=rng.uniform(-50, 50),
                    pitch=rng.uniform(-50, 50),
                    dyaw=rng.uniform(-50, 50),
                    thrust=rng.uniform(-10, 10),
                ),
                CommandSource.POLICY,
            ),
            state_at(world),
        )

    assert len(world.received) == 2000
    for cmd in world.received:
        p = cmd.payload
        assert abs(p.roll) <= env.max_tilt
        assert abs(p.pitch) <= env.max_tilt
        assert abs(p.dyaw) <= env.max_dyaw
        assert env.thrust_min <= p.thrust <= env.thrust_max


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
@pytest.mark.parametrize("field", ["roll", "pitch", "dyaw", "thrust"])
def test_non_finite_command_values_are_refused(field, bad) -> None:
    """NaN and infinity survive every bound written as a comparison.

    ``NaN < low`` and ``NaN > high`` are both false, so clipping returns it
    untouched and a NaN roll used to be deposited with no intervention recorded.
    Random draws can never produce these values, which is why the adversarial test
    above missed them entirely and this one exists.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(cmd_at(world, AttitudeCmd(**{field: bad})), state_at(world))

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_COMMAND,)
    assert field in result.detail
    assert world.received == []


@pytest.mark.parametrize(
    "kw",
    [
        {"pos": np.array([0.0, 0.0, math.nan])},
        {"vel": np.array([math.nan, 0.0, 0.0])},
        {"yaw": math.nan},
        {"t": math.nan},
        {"pos": np.array([0.0, 0.0])},                  # 2-D pos with 3-D vel
        {"pos": np.array([0.0, 0.0, 0.0, 0.0])},        # unsupported dimension
    ],
    ids=["nan-pos", "nan-vel", "nan-yaw", "nan-t", "dim-mismatch", "dim-unsupported"],
)
def test_an_unusable_state_never_authorises_an_emission(kw) -> None:
    """A required observation that cannot be read must not become a default yes.

    ``is_flying`` reads ``pos[2]``; a NaN there makes the comparison false, which
    happens to refuse. That is luck, not a check, and the other fields are not so
    lucky. The state is validated before anything reads it.
    """
    world = RecordingWorld()
    gate = CommandGate(world)
    base = dict(t=world.now, pos=np.array([0.0, 0.0, 5.0]), vel=np.zeros(3), armed=True)
    base.update(kw)

    result = gate.submit(cmd_at(world, AttitudeCmd()), SelfState(**base))

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_STATE,)
    assert world.received == []


def test_a_dead_agent_is_not_commanded() -> None:
    """``alive=False`` means the agent left the run; commanding it is a stale reference."""
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(cmd_at(world, AttitudeCmd()), state_at(world, alive=False))

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_STATE,)
    assert "alive" in result.detail
    assert world.received == []


def test_an_unusable_target_is_refused_rather_than_ignored() -> None:
    """A NaN size reads as "not close" and would silently disable the guard.

    This is the shape of the failure worth naming: a broken perception feed must not
    be indistinguishable from a clear path.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(
        cmd_at(world, AttitudeCmd(pitch=-0.2)),
        state_at(world),
        target_at(world, size=math.nan),
    )

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_TARGET,)
    assert world.received == []


def test_no_target_supplied_is_a_legitimate_state() -> None:
    """``target=None`` means nothing is designated, which is not an error.

    There is nothing to be close to, so the proximity guard has nothing to do. Only
    a target that was supplied and is unusable gets refused.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    assert gate.submit(cmd_at(world, AttitudeCmd(pitch=-0.1)), state_at(world), None).sent


@pytest.mark.parametrize(
    "kw", [{"thrust_min": 0.8, "thrust_max": 0.2}, {"max_tilt": 0.0}, {"max_tilt": math.nan},
           {"size_stop": -1.0}, {"thrust_max": 1.5}, {"max_command_age": 0.0}],
    ids=["inverted-thrust", "zero-tilt", "nan-tilt", "negative-size", "thrust-above-one", "zero-age"],
)
def test_a_nonsensical_envelope_is_refused_at_construction(kw) -> None:
    """A bad configuration is a programmer error, caught where it is written.

    Accepting it and then refusing every command afterwards would report the
    symptom a long way from the cause.
    """
    with pytest.raises(ValueError):
        Envelope(**kw)


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------


def test_an_unstamped_command_is_refused() -> None:
    """``t=None`` cannot be read as fresh, and stamping it on arrival would be a lie.

    Rejuvenating an input at admission makes every command new by definition, which
    is exactly the check being asked for, inverted.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(Command(payload=AttitudeCmd(), t=None), state_at(world))

    assert not result.sent
    assert result.interventions == (Intervention.STALE_COMMAND,)
    assert world.received == []


def test_a_stale_command_is_refused_once_the_clock_moves() -> None:
    """Time is advanced by hand, so the limit is tested rather than approximated."""
    env = Envelope()
    world = RecordingWorld()
    gate = CommandGate(world)
    old = cmd_at(world, AttitudeCmd())

    world.step(env.max_command_age * 0.9)
    assert gate.submit(old, state_at(world)).sent

    world.step(env.max_command_age * 0.5)          # now beyond the limit
    result = gate.submit(old, state_at(world))
    assert not result.sent
    assert result.interventions == (Intervention.STALE_COMMAND,)


def test_a_stale_state_is_refused() -> None:
    """A command produced now, against an observation from a loop that stopped."""
    env = Envelope()
    world = RecordingWorld()
    gate = CommandGate(world)
    old_state = state_at(world)

    world.step(env.max_state_age + 0.01)
    result = gate.submit(cmd_at(world, AttitudeCmd()), old_state)

    assert not result.sent
    assert result.interventions == (Intervention.STALE_STATE,)
    assert world.received == []


def test_a_stored_target_age_does_not_stay_young_while_time_passes() -> None:
    """The probe for ``age`` being a number that does not grow on its own.

    The view below reports ``age=0.1`` forever. Held across a silence longer than
    the limit, reading ``age`` alone would still call it fresh; the gate adds the
    time elapsed since the view was computed and refuses it.
    """
    env = Envelope()
    world = RecordingWorld()
    gate = CommandGate(world)
    view = target_at(world, size=0.05, age=0.1)

    world.step(0.2)
    assert gate.submit(cmd_at(world, AttitudeCmd()), state_at(world), view).sent
    assert view.age == 0.1                                  # unchanged, as designed

    world.step(env.max_target_age)                          # 0.1 + elapsed now exceeds it
    result = gate.submit(cmd_at(world, AttitudeCmd()), state_at(world), view)
    assert not result.sent
    assert result.interventions == (Intervention.STALE_TARGET,)


def test_an_unstamped_locked_target_is_refused() -> None:
    """Without ``t`` there is no way to age ``age``, so the view is unusable."""
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(
        cmd_at(world, AttitudeCmd()),
        state_at(world),
        TargetView(has=True, found=True, size=0.05, t=None),
    )

    assert not result.sent
    assert result.interventions == (Intervention.STALE_TARGET,)


# --------------------------------------------------------------------------
# The proximity guard: what it removes from a command
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", list(CommandSource))
def test_no_source_may_have_a_forward_component_deposited_when_close(source) -> None:
    """The guard covers every path, the operator included.

    This is the defect the module was written to fix: the guard used to sit inside
    the tracking branch, so a human could push the aircraft toward the very target
    the autonomy refused to approach. It removes a command component; it does not
    establish a distance.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(
        cmd_at(world, AttitudeCmd(pitch=-0.20), source),   # nose down: forward
        state_at(world),
        target_at(world, size=0.30),
    )

    assert result.sent
    assert Intervention.PROXIMITY in result.interventions
    assert world.received[-1].payload.pitch == 0.0


def test_the_guard_leaves_the_other_axes_alone() -> None:
    """Only the closing component is removed, never braking, backing off or turning.

    A guard that froze every axis would trade a possible collision for an aircraft
    nobody can recover, which is not an improvement.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(
        cmd_at(world, AttitudeCmd(pitch=0.10, roll=0.05, dyaw=0.02)),  # nose up: braking
        state_at(world),
        target_at(world, size=0.30),
    )

    assert Intervention.PROXIMITY not in result.interventions
    sent = world.received[-1].payload
    assert sent.pitch == pytest.approx(0.10)
    assert sent.roll == pytest.approx(0.05)
    assert sent.dyaw == pytest.approx(0.02)


def test_forward_component_is_removed_above_the_size_threshold() -> None:
    """Commanded hard forward throughout while the apparent size grows.

    Below the threshold the forward component is deposited; at or above it, never.
    The world does not move, so this shows where the transformation switches on and
    nothing about what the aircraft would then do.
    """
    env = Envelope()
    world = RecordingWorld()
    gate = CommandGate(world)

    sizes = [0.02 * step for step in range(1, 16)]  # 0.02 .. 0.30
    for size in sizes:
        gate.submit(
            cmd_at(world, AttitudeCmd(pitch=-0.30)),
            state_at(world),
            target_at(world, size=size),
        )

    for size, cmd in zip(sizes, world.received):
        if size >= env.size_stop:
            assert cmd.payload.pitch == 0.0, f"forward component kept at size {size:.2f}"
        else:
            assert cmd.payload.pitch < 0.0


def test_an_unlocked_target_does_not_trigger_the_guard() -> None:
    """A stale size on a target that is not locked must not brake the aircraft."""
    world = RecordingWorld()
    gate = CommandGate(world)

    gate.submit(
        cmd_at(world, AttitudeCmd(pitch=-0.20)),
        state_at(world),
        target_at(world, has=False, found=False, size=0.90),
    )

    assert world.received[-1].payload.pitch == pytest.approx(-0.20)


@pytest.mark.parametrize(
    "payload, field",
    [(VelocityCmd(vx=99.0), "vx"), (AccelCmd(ax=99.0), "ax")],
    ids=["velocity", "accel"],
)
def test_the_guard_works_in_every_supported_space(payload, field) -> None:
    """Forward is a different field and sign per space; the removal is the same."""
    world = RecordingWorld(space=payload.space)
    gate = CommandGate(world)

    gate.submit(cmd_at(world, payload), state_at(world), target_at(world, size=0.5))

    assert getattr(world.received[-1].payload, field) == 0.0


# --------------------------------------------------------------------------
# Failing closed
# --------------------------------------------------------------------------


def test_nothing_is_deposited_for_an_aircraft_on_the_ground() -> None:
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(cmd_at(world, AttitudeCmd(pitch=-0.3)), state_at(world, alt=0.1))

    assert not result.sent
    assert result.interventions == (Intervention.NOT_FLYING,)
    assert world.received == []


def test_flying_is_measured_not_declared() -> None:
    """Armed is not airborne, and the gate must not confuse the two.

    A takeoff script that sets a flag can be wrong; motors armed at 10 cm are a fact
    that says the aircraft is still on the ground. Height is above the takeoff
    point, not above the terrain.
    """
    env = Envelope()
    ground = np.array([0.0, 0.0, 0.1])
    up = np.array([0.0, 0.0, 2.0])
    assert not is_flying(SelfState(0.0, up, np.zeros(3), armed=False), env)
    assert not is_flying(SelfState(0.0, ground, np.zeros(3), armed=True), env)
    assert is_flying(SelfState(0.0, up, np.zeros(3), armed=True), env)


def test_a_two_dimensional_state_has_no_altitude_to_test() -> None:
    """In 2-D there is no height, so arming is the whole answer and it is stated."""
    env = Envelope()
    assert is_flying(SelfState(0.0, np.zeros(2), np.zeros(2), armed=True), env)
    assert not is_flying(SelfState(0.0, np.zeros(2), np.zeros(2), armed=False), env)


def test_a_command_in_the_wrong_space_is_refused() -> None:
    """A velocity world must never be handed an attitude, silently converted or not."""
    world = RecordingWorld(space=CommandSpace.VELOCITY)
    gate = CommandGate(world)

    result = gate.submit(cmd_at(world, AttitudeCmd(roll=0.1)), state_at(world))

    assert not result.sent
    assert result.interventions == (Intervention.UNSUPPORTED_SPACE,)
    assert world.received == []


def test_body_rates_are_refused_while_no_guard_exists_for_them() -> None:
    """CTBR fails closed, on purpose, and this test is what keeps it honest.

    Zeroing a pitch *rate* holds whatever forward pitch the aircraft already has, so
    the proximity guard cannot be expressed in this space without the current
    attitude. Until SelfState carries it, depositing unguarded body rates is the one
    thing this module exists to prevent.

    When the guard is implemented, this test is the one to delete.
    """
    world = RecordingWorld(space=CommandSpace.CTBR)
    gate = CommandGate(world)

    result = gate.submit(
        cmd_at(world, CtbrCmd(roll_rate=0.1, pitch_rate=-0.5, yaw_rate=0.0, thrust=0.6)),
        state_at(world),
    )

    assert not result.sent
    assert result.interventions == (Intervention.NO_GUARD,)
    assert world.received == []


# --------------------------------------------------------------------------
# The neutral command
# --------------------------------------------------------------------------


def test_hold_is_stamped_now_rather_than_reusing_a_constant() -> None:
    """``hold()`` builds a fresh command, so it can never be stale by construction."""
    world = RecordingWorld()
    gate = CommandGate(world)

    world.step(10.0)
    assert gate.hold(state_at(world)).sent
    assert world.received[-1].payload == NEUTRAL_ATTITUDE
    assert world.received[-1].source is CommandSource.IDLE
    assert world.received[-1].t == world.now


@pytest.mark.parametrize("space", [CommandSpace.VELOCITY, CommandSpace.ACCEL])
def test_hold_is_attitude_only_and_says_so(space) -> None:
    """A world that speaks another space refuses ``hold()``, which is the honest outcome.

    The neutral element of a velocity or acceleration world is a different command,
    and inventing one before a backend exists to fly it would be a guess. The gap is
    visible here rather than hidden behind a conversion.
    """
    world = RecordingWorld(space=space)
    gate = CommandGate(world)

    result = gate.hold(state_at(world))

    assert not result.sent
    assert result.interventions == (Intervention.UNSUPPORTED_SPACE,)
    assert world.received == []


# --------------------------------------------------------------------------
# Bookkeeping the gate must not get wrong
# --------------------------------------------------------------------------


def test_the_gate_never_mutates_what_it_was_handed() -> None:
    """The unclipped command is the record of what was asked for.

    The difference between it and what was deposited is the record of the safety
    layer acting. Mutating in place would erase exactly the events worth reviewing.
    """
    world = RecordingWorld()
    gate = CommandGate(world)
    asked = cmd_at(world, AttitudeCmd(roll=9.9, pitch=-9.9, thrust=9.9), CommandSource.OPERATOR)
    before = asked.payload

    gate.submit(asked, state_at(world), target_at(world, size=0.5))

    assert asked.payload is before
    assert asked.payload == AttitudeCmd(roll=9.9, pitch=-9.9, thrust=9.9)


def test_source_and_timestamp_survive_the_gate() -> None:
    """Clipping must not cost the audit trail its attribution."""
    world = RecordingWorld()
    gate = CommandGate(world)
    asked = cmd_at(world, AttitudeCmd(roll=9.9), CommandSource.OPERATOR)

    gate.submit(asked, state_at(world))

    assert world.received[-1].source is CommandSource.OPERATOR
    assert world.received[-1].t == asked.t


def test_interventions_are_counted() -> None:
    """A gate that never intervenes has either perfect callers or a broken check."""
    world = RecordingWorld()
    gate = CommandGate(world)

    gate.hold(state_at(world))                                          # nothing to do
    assert gate.interventions == 0

    gate.submit(cmd_at(world, AttitudeCmd(roll=9.9)), state_at(world))  # clipped
    gate.submit(cmd_at(world, AttitudeCmd()), state_at(world, alt=0.0))  # withheld
    assert gate.interventions == 2


def test_last_sent_is_on_the_world_clock() -> None:
    """One time base. Reading the wall clock here would mix two unrelated axes."""
    world = RecordingWorld()
    gate = CommandGate(world)
    assert gate.last_sent is None

    world.step(42.0)
    gate.hold(state_at(world))

    assert gate.last_sent == world.now


# --------------------------------------------------------------------------
# Time-base consistency, beyond "not too old"
# --------------------------------------------------------------------------


def test_a_command_stamped_in_the_future_is_refused() -> None:
    """A stamp ahead of the world clock is a second time base, not a fresh input.

    Only a maximum age was checked, so ``t = 10000`` against a clock at 100 read as
    produced 9900 seconds from now and sailed through as very recent.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(
        Command(payload=AttitudeCmd(), source=CommandSource.TRACK, t=world.now + 1000.0),
        state_at(world),
    )

    assert not result.sent
    assert result.interventions == (Intervention.FUTURE_TIMESTAMP,)
    assert "command" in result.detail
    assert world.received == []


def test_a_state_stamped_in_the_future_is_refused() -> None:
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(
        cmd_at(world, AttitudeCmd()),
        SelfState(
            t=world.now + 1000.0,
            pos=np.array([0.0, 0.0, 5.0]),
            vel=np.zeros(3),
            armed=True,
        ),
    )

    assert not result.sent
    assert result.interventions == (Intervention.FUTURE_TIMESTAMP,)
    assert "state" in result.detail
    assert world.received == []


def test_a_future_target_stamp_cannot_mask_an_old_detection() -> None:
    """The case worth naming: a future ``t`` cancels a declared age.

    ``age=100`` with ``t`` far ahead gives an effective age of ``100 + (now - t)``,
    which is deeply negative and passes any maximum. The detection is a hundred
    seconds old and the arithmetic says it is fresher than fresh.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(
        cmd_at(world, AttitudeCmd(pitch=-0.2)),
        state_at(world),
        TargetView(has=True, found=True, size=0.05, age=100.0, t=world.now + 10000.0),
    )

    assert not result.sent
    assert result.interventions == (Intervention.FUTURE_TIMESTAMP,)
    assert "target" in result.detail
    assert world.received == []


def test_a_stamp_inside_a_configured_skew_is_accepted() -> None:
    """The tolerance is a real knob, not decoration, so both sides of it are tested."""
    world = RecordingWorld()
    gate = CommandGate(world, envelope=Envelope(max_clock_skew=0.25))
    ahead = Command(payload=AttitudeCmd(), source=CommandSource.TRACK, t=world.now + 0.1)
    too_far = Command(payload=AttitudeCmd(), source=CommandSource.TRACK, t=world.now + 0.5)

    assert gate.submit(ahead, state_at(world)).sent
    assert gate.submit(too_far, state_at(world)).interventions == (Intervention.FUTURE_TIMESTAMP,)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
def test_an_unusable_world_clock_stops_everything(bad) -> None:
    """A NaN clock does not trip one age limit, it disables all of them at once.

    Every freshness comparison becomes false, so the command travels through with no
    intervention recorded. The clock is therefore checked before it is used.
    """
    world = RecordingWorld()
    gate = CommandGate(world)
    good = cmd_at(world, AttitudeCmd())
    state = state_at(world)
    world.now = bad

    result = gate.submit(good, state)

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_CLOCK,)
    assert "world clock" in result.detail
    assert world.received == []


def test_a_negative_clock_skew_is_refused_at_construction() -> None:
    """Zero is allowed and is the default; below zero is nonsense."""
    assert Envelope(max_clock_skew=0.0).max_clock_skew == 0.0
    with pytest.raises(ValueError, match="max_clock_skew"):
        Envelope(max_clock_skew=-0.1)


# --------------------------------------------------------------------------
# The state has to belong to the world it is acted on in
# --------------------------------------------------------------------------


def test_a_planar_state_is_refused_by_a_three_dimensional_world() -> None:
    """A missing vertical coordinate must not become an authorisation.

    ``is_flying`` reads ``len(pos) < 3`` and answers "armed is enough". Handed a 2-D
    state, a 3-D world therefore skipped the height check entirely: an armed
    aircraft sitting on the ground passed as airborne.
    """
    world = RecordingWorld(dim=3)
    gate = CommandGate(world)
    planar = SelfState(t=world.now, pos=np.zeros(2), vel=np.zeros(2), armed=True)

    assert is_flying(planar, Envelope()) is True          # the branch, in isolation
    result = gate.submit(cmd_at(world, AttitudeCmd()), planar)

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_STATE,)
    assert "2-D" in result.detail and "3-D" in result.detail
    assert world.received == []


def test_a_three_dimensional_state_is_refused_by_a_planar_world() -> None:
    """The converse, so the check is an agreement and not a minimum."""
    world = RecordingWorld(dim=2)
    gate = CommandGate(world)

    result = gate.submit(cmd_at(world, AttitudeCmd()), state_at(world))

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_STATE,)
    assert world.received == []


def test_a_planar_state_is_accepted_by_a_planar_world() -> None:
    """Two dimensions stay supported; only disagreement is refused."""
    world = RecordingWorld(dim=2)
    gate = CommandGate(world)
    planar = SelfState(t=world.now, pos=np.zeros(2), vel=np.zeros(2), armed=True)

    assert gate.submit(cmd_at(world, AttitudeCmd()), planar).sent


# --------------------------------------------------------------------------
# Malformed inputs are refused, never raised
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pos, fragment",
    [
        (np.array(["a", "b", "c"]), "non-numeric dtype"),
        (np.array([1.0, 2.0, 3.0], dtype=object), "non-numeric dtype"),
        (np.array([True, False, True]), "non-numeric dtype"),
        ([[1.0, 2.0], [3.0]], "not convertible"),
        (np.zeros((3, 3)), "must be 1-D"),
    ],
    ids=["strings", "object-dtype", "booleans", "ragged", "two-dimensional"],
)
def test_a_malformed_state_array_is_refused_not_raised(pos, fragment) -> None:
    """The loop must get a refusal it can act on, not an exception it has to catch.

    ``numpy.isfinite`` raises on string and object arrays, so a misconfigured
    producer used to send a ``TypeError`` up through ``submit`` -- the same class of
    failure being guarded against, one level higher.
    """
    world = RecordingWorld()
    gate = CommandGate(world)
    state = SelfState(t=world.now, pos=pos, vel=np.zeros(3), armed=True)

    result = gate.submit(cmd_at(world, AttitudeCmd()), state)

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_STATE,)
    assert fragment in result.detail
    assert world.received == []


@pytest.mark.parametrize(
    "pos",
    [np.zeros(3, dtype=np.float32), np.zeros(3, dtype=np.int64), np.zeros(3, dtype=np.uint8)],
    ids=["float32", "int64", "uint8"],
)
def test_ordinary_numeric_dtypes_stay_accepted(pos) -> None:
    """Tightening the dtype rule must not refuse arrays a real producer emits."""
    world = RecordingWorld()
    gate = CommandGate(world)
    state = SelfState(t=world.now, pos=pos + np.array([0, 0, 5]), vel=np.zeros(3), armed=True)

    assert gate.submit(cmd_at(world, AttitudeCmd()), state).sent


def test_a_numpy_scalar_command_value_is_accepted() -> None:
    """``np.float32(0.1)`` is finite, and the message used to say it was not.

    A producer computing a command from arrays hands over NumPy scalars as a matter
    of course. Refusing them would be a trap, and describing them as non-finite
    would point a reader at the wrong problem.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(cmd_at(world, AttitudeCmd(roll=np.float32(0.1))), state_at(world))

    assert result.sent
    assert world.received[-1].payload.roll == pytest.approx(0.1, abs=1e-6)


def test_a_boolean_command_value_is_refused_as_a_type_not_a_value() -> None:
    """``bool`` is an integer wearing a disguise; a boolean roll angle is a confusion.

    The message has to say which of the two problems it is: an unsupported type
    sends a reader somewhere different from a NaN.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(cmd_at(world, AttitudeCmd(roll=True)), state_at(world))

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_COMMAND,)
    assert "not a real number" in result.detail


def test_a_scalar_outside_the_float_range_is_refused_not_raised() -> None:
    """A Python int has no size limit, so it can be Real and still not become a float.

    ``float(10**400)`` raises ``OverflowError``. The value arrives as a value, not as
    a wrong type, so the answer is a refusal the loop can act on rather than an
    exception it has to catch.
    """
    world = RecordingWorld()
    gate = CommandGate(world)

    result = gate.submit(cmd_at(world, AttitudeCmd(roll=10**400)), state_at(world))

    assert not result.sent
    assert result.interventions == (Intervention.INVALID_COMMAND,)
    assert "supported floating-point range" in result.detail
    assert world.received == []


def test_a_tolerated_forward_stamp_cannot_rejuvenate_a_declared_age() -> None:
    """The non-negative-age argument holds at zero tolerance; the floor makes it hold at any.

    With a positive skew, ``age + (now - t)`` shrinks by up to the tolerance, so a
    view whose producer already declared it too old could be admitted: at
    ``now=100``, ``t=100.2``, ``age=1.1`` and a one-second limit, the arithmetic
    gives 0.9. Flooring the elapsed term at zero keeps the declared age intact.
    """
    world = RecordingWorld()
    gate = CommandGate(world, envelope=Envelope(max_clock_skew=0.25))
    ahead_but_old = TargetView(has=True, found=True, size=0.05, age=1.1, t=world.now + 0.2)

    result = gate.submit(cmd_at(world, AttitudeCmd()), state_at(world), ahead_but_old)

    assert not result.sent
    assert result.interventions == (Intervention.STALE_TARGET,)
    assert "1.100" in result.detail          # the declared age, not the shrunk one
    assert world.received == []


def test_the_default_tolerance_is_zero() -> None:
    """Pinned deliberately: every argument about ages assumes it until widened."""
    assert Envelope().max_clock_skew == 0.0
