"""The law and the safety layer, together, for the first time.

Each module is tested on its own elsewhere. What is checked here is the seam: that
what the law produces is something the gate accepts, and that the gate's guarantees
apply to it exactly as they apply to a human. A contract that holds on both sides in
isolation can still be two contracts.

**Scope.** The world does not move. This establishes what is deposited and what is
refused along the full path; it establishes nothing about trajectories, distances or
contact, which need dynamics and an independent reference.
"""
from __future__ import annotations

import numpy as np
import pytest

from argos.core import Command, CommandSource, CommandSpace, SelfState, TargetView
from argos.guidance import GuidanceGains, VisualGuidance, operator_command
from argos.safety import CommandGate, Envelope, Intervention


class RecordingWorld:
    """Attitude world with a hand-set clock, recording what is deposited."""

    dim = 3

    def __init__(self) -> None:
        self.now = 100.0
        self.received: list[Command] = []

    def command_space(self) -> CommandSpace:
        return CommandSpace.ATTITUDE

    def agents(self):
        return (0,)

    def time(self) -> float:
        return self.now

    def observe(self, agent):
        raise NotImplementedError("no backend yet")

    def command(self, agent, cmd: Command) -> None:
        self.received.append(cmd)

    def step(self, dt: float) -> None:
        self.now += dt


def airborne(world: RecordingWorld) -> SelfState:
    return SelfState(
        t=world.now, pos=np.array([0.0, 0.0, 5.0]), vel=np.zeros(3), armed=True
    )


def target(world: RecordingWorld, **kw) -> TargetView:
    kw.setdefault("has", True)
    kw.setdefault("found", True)
    return TargetView(t=world.now, **kw)


def test_the_law_output_is_accepted_by_the_gate_unchanged() -> None:
    """In ordinary tracking the safety layer should have nothing to do.

    That is the point of giving the law tighter limits than the envelope: an
    intervention is then a signal rather than the normal state of affairs, and the
    counter is worth reading.
    """
    world = RecordingWorld()
    gate = CommandGate(world)
    law = VisualGuidance()

    result = gate.submit(
        law.step(target(world, error_x=0.4, size=0.06), now=world.now, engage=True),
        airborne(world),
        target(world, error_x=0.4, size=0.06),
    )

    assert result.sent
    assert result.interventions == ()
    assert gate.interventions == 0


def test_a_run_of_ordinary_tracking_never_needs_the_envelope() -> None:
    """Fifty cycles of a target moving across the frame, and no clipping."""
    world = RecordingWorld()
    gate = CommandGate(world)
    law = VisualGuidance()

    for i in range(50):
        view = target(world, error_x=np.sin(i * 0.3), size=0.04 + 0.001 * i)
        gate.submit(law.step(view, now=world.now, engage=True), airborne(world), view)
        world.step(0.02)

    assert len(world.received) == 50
    assert not any(Intervention.CLIPPED in c for c in [gate.last.interventions])
    assert gate.interventions == 0


def test_the_proximity_guard_applies_to_the_law_as_well() -> None:
    """The law asks to brake near the target; the gate does not rely on it doing so.

    Two independent things happen to be pointing the same way here, and the test
    keeps them apart: the law backs off because of its own standoff term, and the
    gate would have removed any forward component regardless.
    """
    world = RecordingWorld()
    gate = CommandGate(world)
    law = VisualGuidance()
    close = target(world, size=0.30)

    from_law = law.step(close, now=world.now, engage=True)
    assert from_law.payload.pitch > 0.0                     # the law itself brakes

    forced = Command(
        payload=from_law.payload.__class__(pitch=-0.2),
        source=CommandSource.TRACK,
        t=world.now,
    )
    result = gate.submit(forced, airborne(world), close)

    assert Intervention.PROXIMITY in result.interventions
    assert world.received[-1].payload.pitch == 0.0


def test_the_operator_gets_the_same_treatment_as_the_law() -> None:
    """The regression that motivated a single exit: a human must not bypass the guard."""
    world = RecordingWorld()
    gate = CommandGate(world)
    close = target(world, size=0.30)

    result = gate.submit(operator_command(world.now, forward=1.0), airborne(world), close)

    assert result.sent
    assert Intervention.PROXIMITY in result.interventions
    assert world.received[-1].payload.pitch == 0.0
    assert world.received[-1].source is CommandSource.OPERATOR


def test_a_law_running_on_a_stalled_clock_is_refused_downstream() -> None:
    """The law stamps with the clock it was given, so a stalled loop shows up as stale.

    The law cannot detect that its caller stopped; the gate can, and does. This is
    the freshness contract working across the seam rather than inside one module.
    """
    world = RecordingWorld()
    gate = CommandGate(world)
    law = VisualGuidance()
    stale = law.step(target(world, error_x=0.2), now=world.now, engage=True)

    world.step(Envelope().max_command_age + 0.1)
    result = gate.submit(stale, airborne(world), target(world, error_x=0.2))

    assert not result.sent
    assert result.interventions == (Intervention.STALE_COMMAND,)
    assert world.received == []


def test_an_idle_law_output_still_passes_the_gate() -> None:
    """Losing the target produces a neutral command, which must remain emittable.

    Something has to keep being sent so the autopilot's setpoint timeout does not
    expire; the law's empty-handed output is what fills that gap while a lock is
    being reacquired.
    """
    world = RecordingWorld()
    gate = CommandGate(world)
    law = VisualGuidance()

    result = gate.submit(law.step(TargetView(), now=world.now, engage=True), airborne(world))

    assert result.sent
    assert world.received[-1].source is CommandSource.IDLE


def test_a_diverged_law_is_still_bounded_by_the_envelope() -> None:
    """Gains ten times too large: the gate is what keeps the aircraft flyable.

    The law's own limits scale with its gains, so a badly tuned law bounds itself at
    the wrong place. The envelope does not move.
    """
    env = Envelope()
    world = RecordingWorld()
    gate = CommandGate(world)
    law = VisualGuidance(GuidanceGains(kp_roll=90.0, max_tilt=90.0, max_dyaw=90.0))

    view = target(world, error_x=1.0, size=0.02)
    result = gate.submit(law.step(view, now=world.now, engage=True), airborne(world), view)

    assert result.sent
    assert Intervention.CLIPPED in result.interventions
    assert abs(world.received[-1].payload.roll) == pytest.approx(env.max_tilt)
