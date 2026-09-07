"""The ``World`` protocol: the only object through which a layer touches physics.

**No control layer imports a simulator.** Simulators are used freely, but always
behind this interface, never called directly from perception, guidance, safety or
swarm code. The backends behind it are interchangeable: a NumPy point-mass sim, a
Gazebo and autopilot pair, an airframe flown from the ground over a radio link, an
airframe carrying its own computer. If a port requires touching a control layer,
the interface is leaking and the interface is what gets fixed.

Two details of the protocol are load-bearing.

**The world declares its command space.** A simple integrator accepts velocities;
a quadrotor tilts in order to accelerate, so attitude and translation are coupled
and it has to be talked to in accelerations, then in body rates. Rather than
freezing velocity into every layer and breaking all of them the day real hardware
arrives, each backend answers :meth:`World.command_space` and the safety filter
adapts to the answer.

**Depositing a command and applying it are separate.** :meth:`World.command`
records an intent; :meth:`World.step` applies every recorded intent at once. If the
world applied commands immediately, agent 0 would already have moved inside the
world that agent 1 is about to observe, and the order the loop happens to iterate
in would become a physical parameter of the simulation.

:meth:`World.step` exists on real hardware too, where it waits for the next tick
instead of integrating. One control loop for every backend is worth a method that
does nothing half the time.
"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .command import Command, CommandSpace
from .observation import AgentId, Observation


@runtime_checkable
class World(Protocol):
    """A world an agent can perceive and act on."""

    @property
    def dim(self) -> int:
        """2 or 3. Early phases live in 2D and say so rather than pretending."""
        ...

    def command_space(self) -> CommandSpace:
        """The space this backend accepts. Read this; never assume a constant."""
        ...

    def agents(self) -> Sequence[AgentId]:
        """The agents still alive. Attrition shortens this list."""
        ...

    def time(self) -> float:
        """Seconds since the run started.

        **This is the project's single time base.** ``SelfState.t``, ``Command.t``
        and ``TargetView.t`` are all on this clock, and anything comparing them
        reads it here rather than calling :func:`time.monotonic`. In simulation the
        wall clock is unrelated to simulated time; on hardware this is implemented
        as a monotonic clock started with the run. One consequence worth stating:
        two machines' monotonic clocks share no origin, so a multi-agent backend
        has to define how it relates them rather than assume they line up.
        """
        ...

    def observe(self, agent: AgentId) -> Observation:
        """What this agent perceives. Nothing else, and no global state in disguise."""
        ...

    def command(self, agent: AgentId, cmd: Command) -> None:
        """Record an intent for this agent, to take effect on the next :meth:`step`.

        Three states are distinct and must not be conflated:

        *deposited* -- recorded here, not yet acted on;
        *applied*   -- consumed by a :meth:`step`, and now affecting the vehicle;
        *refused*   -- never deposited, because a caller upstream declined to send.

        A refusal upstream is silence, not an undo. Nothing in this protocol says
        what happens to an intent already deposited when the next one never comes,
        and that gap is deliberate: the answer belongs to a backend, and inventing
        one before any backend exists would be a guess dressed as a contract.

        **Every backend answers three questions, and a caller may not assume the
        answers**: how long a deposit stays valid; whether a later refusal cancels
        an earlier deposit or lets it stand; and whether a stale deposit can be
        applied by surprise after a gap. They stay with the backend because they are
        genuinely different on a simulator, on a link to an autopilot and on a
        companion computer, and because the answers have to be behaviour with tests
        rather than a sentence here. A caller that has not read its backend's
        answers knows only that it sent nothing, which is not the same as knowing
        that nothing is being flown.
        """
        ...

    def step(self, dt: float) -> None:
        """Advance one tick: integrate in simulation, wait for the clock on hardware."""
        ...
