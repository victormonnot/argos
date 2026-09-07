"""Commands: what a layer asks a vehicle to do.

A command is written in one of several *spaces*. The same intent, "go left", is
expressed differently depending on how close to the actuators you are:

    VELOCITY   move at 2 m/s to the left           simple integrator, early sim
    ACCEL      accelerate at 3 m/s2 to the left    double integrator
    ATTITUDE   hold 12 degrees of left bank        hand-written guidance law
    CTBR       roll at 30 deg/s, 65 % thrust       the primitive, real hardware

Two decisions carry this module.

**Payloads are typed, never an anonymous array.** An earlier design passed a
``value`` array whose meaning depended on the space. Reading ``[0.3, -0.2, 0.65]``
then required remembering which index held what, and on an aircraft that flies
toward a person an index mix-up is a collision. Every payload here names its
fields, and no reader has to remember an ordering.

**``space`` is derived, never stored.** Storing both a space tag and a typed
payload lets the two contradict each other, and a command claiming to be a
velocity while carrying body rates would be clipped against the wrong limits.
``Command.space`` reads the payload's own class attribute instead, so the
contradiction is not representable.

Sign conventions, once, so no other module restates them (ArduPilot's, Euler
3-2-1, NED):

    roll  > 0   banks RIGHT, so the aircraft accelerates right
    pitch > 0   is nose UP, so moving FORWARD is a NEGATIVE pitch
    yaw   > 0   turns RIGHT

Yaw is deliberately *not* uniform across spaces, and that is the point:

    VELOCITY, ACCEL, CTBR   ->  ``yaw_rate``, a rate in rad/s
    ATTITUDE                ->  ``dyaw``, an OFFSET in rad from the measured heading

An attitude command never carries an absolute heading. Without a reliable compass
the estimated heading drifts, so a commanded absolute heading would slowly diverge
from the real one and the aircraft would fly a curve it was never asked to fly. An
offset applied to the heading measured at send time cannot drift, because it is
re-anchored on every command.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Union


class CommandSpace(Enum):
    """The space a command is written in.

    Ordered from farthest to closest to the actuators. Each step down is closer to
    the motors and less forgiving than the last. A backend declares which space it
    accepts rather than the caller assuming one, so that moving from a point-mass
    simulation to a real airframe changes a backend and not a control layer.
    """

    VELOCITY = "velocity"
    ACCEL = "accel"
    ATTITUDE = "attitude"
    CTBR = "ctbr"


class CommandSource(Enum):
    """Who produced a command.

    This is audit data, not a label. After a flight that went wrong, the first
    question asked of the log is who was commanding the aircraft at that instant.
    The set is therefore closed: a misspelling is an error at construction rather
    than a silently corrupted answer months later, and adding a new source is a
    deliberate edit rather than a passing string.
    """

    IDLE = "idle"          # nobody asked; the neutral hold command
    TRACK = "track"        # the visual guidance law
    OPERATOR = "operator"  # a human, through the console or the radio
    POLICY = "policy"      # a learned policy


@dataclass(frozen=True)
class VelocityCmd:
    """Body-frame velocity. Accepted by simple-integrator backends."""

    space: ClassVar[CommandSpace] = CommandSpace.VELOCITY

    vx: float = 0.0        # m/s, + forward
    vy: float = 0.0        # m/s, + right
    vz: float = 0.0        # m/s, + up
    yaw_rate: float = 0.0  # rad/s, + turns right


@dataclass(frozen=True)
class AccelCmd:
    """Body-frame acceleration.

    A quadrotor tilts in order to accelerate, so attitude and translation are
    coupled and the holonomic assumption behind VELOCITY stops holding. This is
    the space a safety filter moves into once that assumption is dropped, because
    actuator limits are bounds on acceleration, not on velocity.
    """

    space: ClassVar[CommandSpace] = CommandSpace.ACCEL

    ax: float = 0.0        # m/s2, + forward
    ay: float = 0.0        # m/s2, + right
    az: float = 0.0        # m/s2, + up
    yaw_rate: float = 0.0  # rad/s, + turns right


@dataclass(frozen=True)
class AttitudeCmd:
    """Desired attitude plus collective thrust.

    ``thrust`` is 0..1 and **0.5 means hold altitude**: the autopilot reads it as a
    climb rate and closes the altitude loop on the barometer. That is what keeps
    this space usable with no GPS, no rangefinder and no position estimate of any
    kind, which is the regime the whole project is built for.

    The defaults describe a meaningful neutral: level, heading held, altitude held.
    """

    space: ClassVar[CommandSpace] = CommandSpace.ATTITUDE

    roll: float = 0.0    # rad, + banks right
    pitch: float = 0.0   # rad, + is nose up, so forward is negative
    dyaw: float = 0.0    # rad, OFFSET from the measured heading, never absolute
    thrust: float = 0.5  # 0..1, 0.5 holds altitude


@dataclass(frozen=True)
class CtbrCmd:
    """Collective thrust and body rates: the primitive.

    The smallest common denominator between a rate-controlled guided mode on an
    autopilot and a Betaflight ACRO airframe that has no estimator at all, and the
    action space learned policies are written in. It is part of the contract from
    the first commit specifically so it never has to be retrofitted later, when
    every caller would have to change at once.

    **No field has a default.** Unlike ATTITUDE there is no meaningful neutral
    here: ``thrust`` is a raw collective, so a default of zero would make
    ``CtbrCmd()`` mean "cut the motors" and read like "nothing in particular".
    A rate command is either fully specified or it is a mistake.
    """

    space: ClassVar[CommandSpace] = CommandSpace.CTBR

    roll_rate: float   # rad/s, body axis, + banks right
    pitch_rate: float  # rad/s, body axis, + is nose up
    yaw_rate: float    # rad/s, + turns right
    thrust: float      # 0..1, raw collective


Payload = Union[VelocityCmd, AccelCmd, AttitudeCmd, CtbrCmd]
"""Anything a :class:`Command` can carry. Adding a space means adding a payload
type here and a member to :class:`CommandSpace`, and every exhaustive match over
commands will then fail to compile or fail its test until it handles the new one."""


@dataclass(frozen=True)
class Command:
    """A payload plus the facts that belong to every command whatever its space.

    The wrapper exists for what is genuinely cross-cutting: who produced the
    command, and when. Those would otherwise be repeated in all four payloads and
    would drift apart.

    ``space`` is a property rather than a field, so a command cannot claim one
    space while carrying another.
    """

    payload: Payload
    source: CommandSource = CommandSource.IDLE
    t: float | None = None
    """When this command was produced, on the clock of the world it is bound for
    (:meth:`argos.core.World.time`). ``None`` means unstamped.

    ``None`` is **not** a free pass. A gate that enforces freshness has to refuse an
    unstamped command, because it has no way to tell a fresh one from an old one,
    and stamping it on arrival would make every command look new by definition.
    """

    def __post_init__(self) -> None:
        # Type errors are programmer errors and are caught here, at construction,
        # where the traceback points at the line that got it wrong. Data errors
        # (a NaN, a stale timestamp) are runtime conditions and belong to the
        # admission boundary instead, which can refuse safely; see argos.safety.
        if not isinstance(self.source, CommandSource):
            raise TypeError(
                f"source must be a CommandSource, got {type(self.source).__name__}: "
                f"{self.source!r}"
            )

    @property
    def space(self) -> CommandSpace:
        """The space this command is written in, read from the payload itself."""
        return self.payload.space

    def replace(self, **fields: float) -> Command:
        """A copy with some payload fields changed, leaving this one untouched.

        The safety filter clips commands, and it must never mutate what it was
        handed: the unclipped command is what gets logged as "what was asked for",
        and the difference between the two is the record of the filter intervening.
        Losing that difference would hide exactly the events worth reviewing.
        """
        return Command(
            payload=dataclasses.replace(self.payload, **fields),
            source=self.source,
            t=self.t,
        )


NEUTRAL_ATTITUDE = AttitudeCmd()
"""The neutral attitude payload: level, heading held, altitude held.

Deliberately a **payload and not a ready-made** :class:`Command`. A module-level
Command would carry a fixed ``t`` forever, so it would either be permanently stale
or have to be exempted from the freshness rule, and a standing exemption on the
one command emitted most often is the last place to want one. Callers wrap this in
a Command stamped with the current time; :meth:`argos.safety.CommandGate.hold` does
exactly that.

What it is for: something has to be emitted continuously so a guided mode's
setpoint timeout never expires by accident. An autopilot that stops receiving
setpoints takes control back, which is correct behaviour and must stay, but it has
to be a decision rather than a side effect of the loop being briefly busy.

What it does **not** claim: this holds attitude and commands the altitude loop to
hold. It says nothing about horizontal position. With no position estimate the
aircraft drifts with the wind while flying a perfectly neutral attitude.
"""
