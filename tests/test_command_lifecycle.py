"""What actually happens to an intent, from the law to the aircraft and back to rest.

Everything before this lot could only say ``sent=False``. That was an honest report
and a nearly useless one: it said nothing new was deposited, and nothing at all
about what the aircraft was doing meanwhile. With a backend that keeps intents,
those questions have answers, and they are answers about behaviour rather than
about a return value.

The three the protocol left open, exercised here through the real chain:

    how long does a deposit stay valid    ->  AttitudeSim.command_lifetime
    does a refusal cancel it              ->  no; it expires, it is not cancelled
    can a stale deposit act by surprise   ->  no; expiry drops it

**Perception is not in this lot.** The one test that runs the guidance law drives it
from a scripted sequence of views that does not react to the aircraft's motion. It
demonstrates that a command travels law -> gate -> world -> motion. It demonstrates
nothing about a camera, and no closed perception loop exists yet.
"""
from __future__ import annotations

import math

import pytest

from argos.backends import Applied, AttitudeSim, Refusal, Vehicle
from argos.core import AttitudeCmd, Command, CommandSource, TargetView
from argos.guidance import VisualGuidance
from argos.safety import CommandGate, Envelope, Intervention

HZ = 50.0
DT = 1.0 / HZ

TICK = 1.0 / 64.0
"""An exactly representable step, used wherever a deadline is being asserted."""

FORWARD = AttitudeCmd(pitch=-0.12)


def track(payload: AttitudeCmd, t: float | None) -> Command:
    return Command(payload=payload, source=CommandSource.TRACK, t=t)


def test_an_accepted_command_travels_all_the_way_to_motion() -> None:
    """The baseline the rest is measured against: the chain moves the aircraft."""
    world = AttitudeSim()
    gate = CommandGate(world)

    for _ in range(50):
        state = world.observe(0).me
        assert gate.submit(track(FORWARD, world.time()), state).sent
        world.step(DT)

    assert world.observe(0).me.vel[1] > 0.5
    assert world.applied[Applied.FRESH] == 50


def test_a_refusal_deposits_nothing_and_the_world_sees_nothing() -> None:
    """The two doors do not overlap: what the gate withholds never reaches the world.

    Worth its own test because the world has a door of its own, and a refusal
    counted in the wrong place would make one of the two look like it was working.
    """
    world = AttitudeSim()
    gate = CommandGate(world)

    result = gate.submit(track(FORWARD, None), world.observe(0).me)

    assert not result.sent
    assert result.interventions == (Intervention.STALE_COMMAND,)
    assert world.standing is None
    assert sum(world.refusals.values()) == 0


def test_a_refusal_does_not_cancel_the_intent_already_deposited() -> None:
    """The answer the protocol was waiting for, as behaviour.

    A refusal upstream is silence, and silence does not reach back. The aircraft
    keeps flying the last accepted command, and reading ``sent=False`` as "the
    aircraft is not being commanded" is wrong in exactly the situation where being
    wrong matters.
    """
    world = AttitudeSim(command_lifetime=0.5)
    gate = CommandGate(world)

    assert gate.submit(track(FORWARD, world.time()), world.observe(0).me).sent
    world.step(DT)
    speed_then = world.observe(0).me.vel[1]

    for _ in range(10):
        result = gate.submit(track(FORWARD, None), world.observe(0).me)   # unstamped
        assert not result.sent
        world.step(DT)

    assert world.last_applied is Applied.HELD
    assert world.observe(0).me.vel[1] > speed_then      # still accelerating, refused


def test_the_refusal_is_bounded_by_the_lifetime_and_not_by_the_caller() -> None:
    """Silence starts a timer. This is where it runs out.

    What makes ``sent=False`` reportable at all: the aircraft is flying the previous
    intent for at most ``command_lifetime`` more seconds, and after that this world
    flies its own neutral whatever the caller does.
    """
    world = AttitudeSim(command_lifetime=0.25, max_step=TICK)
    gate = CommandGate(world)

    assert gate.submit(track(FORWARD, world.time()), world.observe(0).me).sent

    for _ in range(16):                                  # 16 * 1/64 = 0.25 exactly
        assert not gate.submit(track(FORWARD, None), world.observe(0).me).sent
        world.step(TICK)
    assert world.last_applied is Applied.HELD

    world.step(TICK)
    assert world.last_applied is Applied.EXPIRED
    assert world.expired_at == 0.25
    assert world.standing is None


def test_the_two_freshness_bounds_compose_and_the_sum_is_the_number_that_matters() -> None:
    """The gate bounds admission age; the world bounds how long an admission acts.

    They measure different things and neither is derived from the other, so the real
    worst case is their sum: an intent computed at ``t`` can still be flying the
    aircraft at ``t + max_command_age + command_lifetime``. A reader who knew only
    one of the two numbers would be out by the other.

    Steps of 0.25 s are exact in binary, so this asserts the boundary itself rather
    than a boundary blurred by accumulated rounding.
    """
    lifetime = 0.5
    envelope = Envelope(max_command_age=0.5)
    world = AttitudeSim(t0=0.5, command_lifetime=lifetime, max_step=0.25)
    gate = CommandGate(world, envelope=envelope)

    computed_at = 0.0
    submitted_at = world.time()
    assert submitted_at - computed_at == envelope.max_command_age
    assert gate.submit(track(FORWARD, computed_at), world.observe(0).me).sent, (
        "a command of exactly max_command_age is still admissible"
    )

    # Fly until the intent stops acting. The instant is read from the world, which
    # reports the deadline itself and not the start of the step that noticed it: an
    # earlier version of this test measured the latter, and its conclusion about how
    # long the intent acted was therefore up to one step stronger than its evidence.
    for _ in range(20):
        world.step(0.25)
        if world.last_applied is Applied.EXPIRED:
            break

    assert world.last_applied is Applied.EXPIRED
    assert world.expired_at == pytest.approx(
        computed_at + envelope.max_command_age + lifetime
    )


def test_holding_is_what_keeps_the_setpoint_alive() -> None:
    """Why a neutral command has to be emitted rather than nothing.

    ``hold`` is not decoration: a loop with nothing to say still has to say it, or
    the world stops receiving intents and the timer starts. On an autopilot the same
    silence hands control back, which is correct behaviour and must remain a
    decision rather than a consequence of the loop being briefly busy.
    """
    world = AttitudeSim(command_lifetime=0.2)
    gate = CommandGate(world)

    for _ in range(100):                                   # two seconds of holding
        assert gate.hold(world.observe(0).me).sent
        world.step(DT)

    assert world.applied[Applied.EXPIRED] == 0
    assert world.applied[Applied.FRESH] == 100
    assert world.observe(0).me.pos[2] == pytest.approx(5.0)


def test_a_loop_that_stops_holding_expires_within_the_lifetime() -> None:
    """The control for the test above: stop, and the timer really does run."""
    world = AttitudeSim(command_lifetime=0.2)
    gate = CommandGate(world)

    for _ in range(10):
        gate.hold(world.observe(0).me)
        world.step(DT)
    assert world.applied[Applied.EXPIRED] == 0

    for _ in range(20):                                    # the loop goes quiet
        world.step(DT)
    assert world.applied[Applied.EXPIRED] == 1
    assert world.last_applied is Applied.IDLE


def test_the_proximity_guard_reaches_the_aircraft_and_not_only_the_result() -> None:
    """A guard that only changed a returned object would guard nothing.

    The forward component is removed on the way through the gate, so what the world
    receives, and therefore what the aircraft flies, is the guarded command. Asserted
    on the motion rather than on ``result.cmd``, which the gate controls either way.

    This bounds a command. It is not a statement about distance kept: see
    :attr:`argos.safety.Envelope.size_stop`.
    """
    size = Envelope().size_stop * 2.5          # unambiguously inside the threshold
    world = AttitudeSim()
    gate = CommandGate(world)

    for _ in range(50):
        view = TargetView(has=True, found=True, size=size, t=world.time())
        result = gate.submit(track(FORWARD, world.time()), world.observe(0).me, view)
        assert result.sent
        assert Intervention.PROXIMITY in result.interventions
        world.step(DT)

    assert world.observe(0).me.vel[1] == pytest.approx(0.0, abs=1e-9)
    assert world.attitude[1] == pytest.approx(0.0, abs=1e-9)


def test_the_guidance_law_flies_the_aircraft_through_the_gate() -> None:
    """Law -> gate -> world -> motion, with the real objects and no stand-ins.

    **The views are scripted.** They are a fixed sequence that does not react to
    where the aircraft goes, because no camera model exists yet and inventing one
    here would make this test look like a closed perception loop that it is not.
    What it establishes is that a command produced by the law reaches the aircraft
    and moves it in the commanded direction.

    A target held to the right of the image centre: the law banks right and turns
    right, and the aircraft goes right of its initial heading.
    """
    world = AttitudeSim(yaw=0.0)
    gate = CommandGate(world)
    law = VisualGuidance()

    for _ in range(100):
        now = world.time()
        view = TargetView(has=True, found=True, error_x=0.3, size=0.05, t=now)
        cmd = law.step(view, now=now, engage=True)
        assert gate.submit(cmd, world.observe(0).me, view).sent
        world.step(DT)

    state = world.observe(0).me
    assert state.yaw > math.radians(5)          # it turned toward the target
    assert state.pos[0] > 0.1                   # and moved east, which is right
    assert world.applied[Applied.EXPIRED] == 0


def test_a_law_that_stops_producing_does_not_leave_the_aircraft_committed() -> None:
    """The whole point, in one sequence.

    The law flies the aircraft, then stops. Nothing cancels anything. The intent
    expires on its own, the world returns to its neutral, and the aircraft coasts to
    a stop on drag alone rather than holding the last attitude forever.
    """
    world = AttitudeSim(command_lifetime=0.3, vehicle=Vehicle(drag=0.8))
    gate = CommandGate(world)
    law = VisualGuidance()

    for _ in range(60):
        now = world.time()
        view = TargetView(has=True, found=True, error_x=0.0, size=0.05, t=now)
        assert gate.submit(law.step(view, now=now, engage=True), world.observe(0).me, view).sent
        world.step(DT)
    assert world.observe(0).me.vel[1] > 0.3

    for _ in range(500):                        # the law is gone; nobody says stop
        world.step(DT)

    assert world.applied[Applied.EXPIRED] == 1
    assert world.last_applied is Applied.IDLE
    assert world.observe(0).me.vel[1] == pytest.approx(0.0, abs=1e-3)


def test_an_aircraft_on_the_ground_is_refused_and_the_world_is_handed_nothing() -> None:
    """The flying check is the gate's, and the world never hears about the command.

    Run against the real backend rather than a stub, because "the gate refused" and
    "the vehicle received nothing" are two facts and only the second one matters to
    the aircraft.
    """
    world = AttitudeSim(pos=[0.0, 0.0, 0.2])           # below min_flying_alt
    gate = CommandGate(world)

    result = gate.submit(track(FORWARD, world.time()), world.observe(0).me)

    assert not result.sent
    assert result.interventions == (Intervention.NOT_FLYING,)
    assert world.standing is None
    assert sum(world.refusals.values()) == 0


def test_holding_after_a_refusal_is_what_a_loop_is_supposed_to_do() -> None:
    """The discipline the lot argues for, shown against the alternative.

    Left to itself a refused loop goes silent and the aircraft flies its last intent
    until the timer runs out. A loop that answers a refusal with ``hold`` levels the
    aircraft *by decision*, at the cycle it decided, instead of some fraction of a
    second later because a bound happened to elapse. Both are safe; only one of them
    is a choice.
    """
    world = AttitudeSim(command_lifetime=0.5)
    gate = CommandGate(world)

    for _ in range(25):
        gate.submit(track(FORWARD, world.time()), world.observe(0).me)
        world.step(DT)
    speed = world.observe(0).me.vel[1]
    assert speed > 0.3

    for _ in range(50):
        state = world.observe(0).me
        assert not gate.submit(track(FORWARD, None), state).sent   # the law is refused
        assert gate.hold(state).sent                               # and says so anyway
        world.step(DT)

    assert world.applied[Applied.EXPIRED] == 0        # the timer never had to run
    assert world.attitude[1] == pytest.approx(0.0, abs=1e-3)
    assert world.observe(0).me.vel[1] < speed         # slowing on drag alone


def test_a_gate_pointed_at_an_agent_its_world_does_not_have_is_refused() -> None:
    """An assembly mistake that used to report success over a command nobody received.

    The gate defaults to agent 0; this world only has agent 7. Every check in
    ``submit`` passed, the world declined the unknown id in silence -- it has no
    return channel and is not supposed to have one -- and the result said ``sent``
    with ``last_sent`` updated. Two parts of the same chain gave two different
    answers about whether a command existed.

    Caught at construction rather than papered over per call: it cannot become true
    later, and the traceback points at the line that assembled the pair.
    """
    world = AttitudeSim(agent=7)

    with pytest.raises(ValueError) as raised:
        CommandGate(world)

    assert "7" in str(raised.value)
    assert sum(world.refusals.values()) == 0        # nothing was even attempted


def test_the_matching_pair_deposits_normally() -> None:
    """The control: name the same agent and the chain works exactly as before."""
    world = AttitudeSim(agent=7)
    gate = CommandGate(world, agent=7)

    result = gate.submit(track(FORWARD, world.time()), world.observe(7).me)

    assert result.sent
    assert world.standing is not None
    assert gate.last_sent == world.time()

    world.step(DT)
    assert world.last_applied is Applied.FRESH


def test_the_backend_keeps_declining_a_command_that_was_never_meant_for_it() -> None:
    """The gate check does not replace the backend's own door.

    Nothing forces a caller to go through a gate, and a world that trusted the
    assembly check to have happened would integrate a command addressed to somebody
    else. Both stay.
    """
    world = AttitudeSim(agent=7)
    world.command(0, track(FORWARD, world.time()))

    assert world.standing is None
    assert world.refusals[Refusal.UNKNOWN_AGENT] == 1
