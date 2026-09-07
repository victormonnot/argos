"""The first backend: a vehicle that is flown by tilting, simulated.

This is the first implementation of :class:`argos.core.World`, and its job is as
much to *settle a contract* as to move a point around. Three questions were left
open in the protocol on purpose, because answering them before any backend existed
would have been a guess dressed as a contract. They are answered here, and they are
answered as behaviour with tests, not as prose:

**How long does a deposit stay valid?** ``command_lifetime`` seconds, measured from
the instant *this world* recorded it. Not from ``Command.t``: that field is the
caller's claim about its own clock, and a backend that measured a lifetime from it
would let a caller with a wrong clock extend how long its intent keeps flying the
aircraft. The world stamps the deposit with the one reading it is sure of, its own.

**The deadline is honoured inside the step that crosses it.** A step spanning the
deadline is integrated in two pieces: the deposit up to the instant it expires, the
neutral for the remainder. Expiring only on step boundaries would have made the real
bound ``command_lifetime + max_step`` while the documentation said
``command_lifetime``, and a bound that holds only to within one step is not the
bound that was announced. It is also the more faithful model: an autopilot's setpoint
timeout fires on its own clock, not on the cadence of whatever is talking to it.

**Does a refusal upstream cancel a deposit already made?** No. A refusal is silence,
and silence does not reach back. The standing deposit keeps acting until it expires,
after which this world flies its own neutral. That is what an autopilot does with a
guided setpoint, and it is why :meth:`argos.safety.CommandGate.hold` exists: a loop
that stops emitting does not stop the aircraft, it starts a timer.

The useful consequence is that ``sent=False`` stops being an unbounded unknown. It
now means: nothing new was deposited, and whatever was deposited before is still
being flown for at most ``command_lifetime`` more seconds.

**Can a stale deposit be applied by surprise after a gap?** No. Expiry is evaluated
at the instant of application and an expired deposit is *dropped*, not skipped, so
there is no state left for a later step to find. A deposit that has stopped acting
can never start again.

**One clock caveat worth stating.** In this backend time advances only inside
:meth:`step`, so a deposit cannot age while the loop is stopped: a paused world is
paused for the deposit too. On a hardware backend the clock runs whether or not the
loop does, and a stalled loop ages its own setpoint into expiry. Code that must
behave the same on both reads :meth:`time` and never assumes which one it is on.

**Nothing here is random.** No noise, no jitter, no sampling: a run is a function
of its inputs, so a trajectory that differs between two runs differs because the
inputs did. That is a property of this backend and not yet of the project, which
still has no seeding discipline of its own.

**What this simulation is.** A point mass that accelerates by tilting, with the
heading and altitude loops closed by a stand-in autopilot, which is exactly the
vehicle an ATTITUDE command describes. Attitude follows the command through a
first-order lag, because no real airframe reaches a commanded bank instantly and a
law tuned against one that does will not transfer.

**What it is not, and no result taken here may claim otherwise.** There is no
aerodynamic model beyond one lumped linear drag term, no motor dynamics, no wind,
no ground effect, no contact dynamics, no sensor, no estimator, no noise, no
takeoff, no landing and no mode machine. None of its parameters were identified
against a real airframe. It is a **command-path** simulation: it is evidence about
when an intent acts and stops acting, and it is not evidence about what distance
the aircraft keeps from anything. Establishing separation needs a dynamics run
against an independent reference, which nothing here provides.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

import numpy as np

from argos.core import (
    AgentId,
    AttitudeCmd,
    Command,
    CommandSpace,
    NEUTRAL_ATTITUDE,
    Observation,
    SelfState,
)
from argos.safety.validate import check_payload, check_state

GRAVITY = 9.80665  # m/s2

DEG = math.pi / 180.0


class Applied(Enum):
    """What the world actually flew during a step.

    Instrumentation, like :class:`argos.safety.Intervention`, and the field to read
    when asking why the aircraft did something after the loop went quiet. The four
    are distinct events and collapsing any two of them would hide the moment an
    intent stopped acting.
    """

    IDLE = "idle"        # nothing is deposited: the world flew its own neutral
    FRESH = "fresh"      # a deposit recorded since the last step
    HELD = "held"        # the same deposit again, still inside its lifetime
    EXPIRED = "expired"  # a deposit outlived its lifetime; dropped, neutral flown


class Refusal(Enum):
    """Why the world declined to record a deposit.

    A backend is not a safety layer and does not decide whether a command is a good
    idea. It does decline what it cannot integrate at all, because a ``NaN`` that
    reaches the integrator is not one bad step, it is every step afterwards.
    """

    UNKNOWN_AGENT = "unknown_agent"
    INVALID_COMMAND = "invalid_command"
    UNSUPPORTED_SPACE = "unsupported_space"


@dataclass(frozen=True)
class Vehicle:
    """The airframe and its inner loops, lumped as the ATTITUDE interface sees them.

    Airframe and autopilot are deliberately not separated here. An attitude command
    is handed to a vehicle that already closes its own attitude, heading and
    altitude loops, so from this side the two are one object. Splitting them would
    invent a boundary this backend cannot observe.

    **These are plausible numbers, not identified ones.** No parameter here was
    measured against a real aircraft, and none should be quoted as one.
    """

    drag: float = 0.6
    """1/s, lumped linear drag. It is what gives the model a terminal speed:
    holding a tilt converges to ``g * tan(tilt) / drag`` rather than accelerating
    without bound. Zero is accepted, and means an aircraft that never stops
    speeding up; useful to isolate a test, wrong as a description of anything."""

    tau_attitude: float = 0.15
    """s, first-order lag of the actual attitude behind the commanded one. Zero
    means attitude arrives instantly, which no airframe does; it exists so a test
    can remove the lag deliberately rather than a law being tuned as if it did."""

    tau_climb: float = 0.5
    """s, lag of the vertical speed behind the climb rate the thrust asks for."""

    max_climb_rate: float = 2.0
    """m/s reached at full thrust deflection. :class:`argos.core.AttitudeCmd` says
    thrust is read as a climb rate with 0.5 holding altitude, and this is the scale
    that reading uses: ``vz_target = (thrust - 0.5) * 2 * max_climb_rate``."""

    yaw_rate: float = 1.2
    """rad/s, the slew rate of the heading loop toward a commanded heading."""

    model_tilt_limit: float = 60.0 * DEG
    """rad, where this model stops describing anything.

    Horizontal acceleration is ``g * tan(tilt)``, which runs to infinity at ninety
    degrees, and a quadrotor near that attitude is not holding altitude anyway. The
    tilt used by the model is clamped here so that a caller who never passed through
    a safety envelope gets a bounded wrong answer instead of an unbounded one."""

    def __post_init__(self) -> None:
        """Reject a configuration that cannot describe a vehicle.

        At construction, like :class:`argos.safety.Envelope`: a simulation whose
        parameters are nonsense produces numbers that look like results.
        """
        for name in ("drag", "tau_attitude", "tau_climb"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"Vehicle.{name} must be finite and not negative, got {value!r}"
                )
        for name in ("max_climb_rate", "yaw_rate"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"Vehicle.{name} must be finite and positive, got {value!r}"
                )
        if not math.isfinite(self.model_tilt_limit) or not 0.0 < self.model_tilt_limit < math.pi / 2:
            raise ValueError(
                f"Vehicle.model_tilt_limit must be finite and within (0, pi/2), "
                f"got {self.model_tilt_limit!r}"
            )


@dataclass(frozen=True)
class Deposit:
    """One recorded intent, with everything needed to keep flying it.

    ``yaw_target`` is the reason this type exists rather than a bare command.
    :class:`argos.core.AttitudeCmd` carries ``dyaw``, an offset from *the heading
    measured when the command was encoded*, and that anchor is a fact about the
    moment of deposit. Re-reading the current heading every time the deposit is
    applied would re-anchor the offset on each step and turn a fixed offset into a
    steady turn: a command meaning "five degrees right" would mean "five degrees
    right per step, forever". The anchor is therefore taken once, here, and the
    deposit carries the absolute heading it resolved to.
    """

    cmd: Command
    at: float          # world clock when this world recorded it
    seq: int           # strictly increasing; distinguishes a new deposit from a held one
    yaw_target: float  # rad, absolute heading, resolved against the anchor at deposit


def _wrap(angle: float) -> float:
    """``angle`` brought into (-pi, pi].

    The half-open interval is closed at ``+pi`` rather than at ``-pi`` so that a
    heading of exactly half a turn has one representation instead of two that
    compare unequal.
    """
    wrapped = (angle + math.pi) % (2.0 * math.pi) - math.pi
    return math.pi if wrapped == -math.pi else wrapped


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


class AttitudeSim:
    """A single aircraft, flown in ATTITUDE, integrated in three dimensions.

    Implements :class:`argos.core.World`, and also :class:`argos.core.Truth`,
    because in simulation the authoritative answer is the integrator's own state.
    Control layers cannot reach the second one: ``tests/test_core_isolation.py``
    enumerates the packages allowed to import ground truth and fails the build for
    every other one.

    **One agent.** ``agents()`` returns a single id and a command for any other is
    refused. A multi-agent world is a different backend with its own decisions about
    what agents perceive of each other, and faking one here by looping over a list
    would produce a swarm simulation that was never designed.
    """

    def __init__(
        self,
        pos: Sequence[float] | np.ndarray | None = None,
        vel: Sequence[float] | np.ndarray | None = None,
        yaw: float = 0.0,
        armed: bool = True,
        agent: AgentId = 0,
        vehicle: Vehicle | None = None,
        command_lifetime: float = 0.5,
        max_step: float = 0.1,
        t0: float = 0.0,
    ) -> None:
        """``pos`` and ``vel`` are ENU metres from takeoff; see :class:`argos.core.SelfState`.

        ``command_lifetime`` is **this backend's policy** and is not derived from
        :class:`argos.safety.Envelope`. The two bound different things and both are
        needed: the envelope bounds how old a command may be when it is *admitted*,
        this bounds how long an admitted command keeps *acting*. They compose, so an
        intent can still be flying up to ``max_command_age + command_lifetime``
        seconds after it was computed, and that sum is the number worth knowing. It
        is exact rather than approximate: the deadline is honoured mid-step, so the
        sum does not have to be widened by a step to stay true.

        **It bounds the command, not the response, and the difference is not
        rhetorical.** Expiry returns the *commanded* attitude to neutral; the actual
        attitude then decays toward it over ``Vehicle.tau_attitude``, and the
        aircraft keeps accelerating the whole time. Speed peaks after the intent has
        stopped, not when it stops. Reading this bound as "the aircraft is no longer
        doing what it was told" is wrong by roughly a time constant, and on a real
        airframe by rather more than this model's.

        ``max_step`` bounds ``dt``. The integrator is explicit Euler, so a large
        step is not a coarse answer but a wrong one, and a loop that hands over a
        step that big has usually lost its cadence rather than chosen it.
        """
        self._vehicle = vehicle or Vehicle()
        self._agent = agent

        for name, value in (("command_lifetime", command_lifetime), ("max_step", max_step)):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"AttitudeSim.{name} must be finite and positive, got {value!r}"
                )
        self._command_lifetime = float(command_lifetime)
        self._max_step = float(max_step)

        if not math.isfinite(t0):
            raise ValueError(f"AttitudeSim.t0 must be finite, got {t0!r}")
        self._t = float(t0)

        self._pos = np.array([0.0, 0.0, 5.0] if pos is None else pos, dtype=float)
        self._vel = np.zeros(3) if vel is None else np.array(vel, dtype=float)
        self._yaw = yaw
        self._armed = bool(armed)

        # Validated with the same predicate the safety layer uses, so "usable state"
        # has one definition rather than one per layer. A bad initial condition is a
        # programmer error and raises here, where the traceback points at it.
        problem = check_state(self._state(), dim=3)
        if problem:
            raise ValueError(f"AttitudeSim cannot start from this state: {problem}")
        self._yaw = _wrap(float(yaw))

        # Actual attitude, lagging behind whatever was commanded. Internal to the
        # model: SelfState carries no attitude, and this is not part of the World
        # contract. Exposed read-only because a test that cannot see it can only
        # check the lag through its consequences.
        self._roll = 0.0
        self._pitch = 0.0

        self._deposit: Deposit | None = None
        self._seq = 0
        self._applied_seq: int | None = None

        self.last_applied = Applied.IDLE
        self.last_flown: AttitudeCmd = NEUTRAL_ATTITUDE
        """The payload being flown at the **end** of the last step, which is not
        always the one last deposited: after an expiry it is this world's own
        neutral. The question "what was the aircraft doing when the loop went quiet"
        has no other answer, and it is the question this lot exists to make
        answerable.

        On the step that crosses a deadline the aircraft flew two payloads, and this
        reports the second. :attr:`expired_at` is what says where the handover fell.
        """

        self.expired_at: float | None = None
        """World-clock instant at which a deposit last stopped acting, or ``None``.

        Exact, and it is the instant the promise is about: it falls inside a step
        whenever the deadline does. Reading the start of the step that reported
        ``EXPIRED`` would be off by up to one step, which is precisely the error this
        field exists to make impossible to repeat."""

        self.applied: dict[Applied, int] = {kind: 0 for kind in Applied}
        """How many steps flew each kind of intent. A run whose ``EXPIRED`` count is
        not zero had a loop that stopped emitting, which is worth seeing."""

        self.refusals: dict[Refusal, int] = {kind: 0 for kind in Refusal}
        self.last_refusal = ""
        self.overwritten = 0
        """Deposits replaced by a newer one before any step applied them. This is a
        stream of setpoints and not a queue, so the later intent wins and the earlier
        one never acts; a loop depositing twice per step is depositing one command
        that does nothing."""

        self.steps = 0

    # --- the World protocol ------------------------------------------------

    @property
    def dim(self) -> int:
        return 3

    def command_space(self) -> CommandSpace:
        return CommandSpace.ATTITUDE

    def agents(self) -> tuple[AgentId, ...]:
        return (self._agent,)

    def time(self) -> float:
        """Seconds since ``t0``. Advanced only by :meth:`step`; see the module note."""
        return self._t

    def observe(self, agent: AgentId) -> Observation:
        """What the aircraft perceives.

        ``neighbors`` is empty: one agent. ``target`` is ``None``: this backend
        carries no sensor model, and a target view has to come from perception
        rather than from a world handing over something it already knows.
        """
        self._require_known(agent)
        return Observation(me=self._state(), neighbors=(), target=None)

    def command(self, agent: AgentId, cmd: Command) -> None:
        """Record an intent, replacing any intent not yet expired.

        Declines rather than raises. A backend is at the end of a link, and on that
        link a malformed message is an ordinary event: raising would turn one bad
        command into a stopped control loop, which is a worse outcome than ignoring
        it. What is declined is only what cannot be integrated at all -- unreadable
        numbers, the wrong command space, an agent that does not exist -- never a
        command that is merely a bad idea, which is the safety layer's judgement to
        make and not this one's.

        **There is no return channel, and that is the contract rather than an
        omission.** :meth:`argos.core.World.command` returns nothing because a
        setpoint on a real link is not acknowledged either. A refusal is therefore
        visible only in :attr:`refusals` and :attr:`last_refusal`, which is where a
        loop or a test looks for it.
        """
        if agent != self._agent:
            self._refuse(Refusal.UNKNOWN_AGENT, f"no agent {agent!r} in this world")
            return

        problem = check_payload(cmd)
        if problem:
            self._refuse(Refusal.INVALID_COMMAND, problem)
            return

        if cmd.space is not CommandSpace.ATTITUDE:
            self._refuse(
                Refusal.UNSUPPORTED_SPACE,
                f"this world speaks attitude, got {cmd.space.value}",
            )
            return

        if self._deposit is not None and self._deposit.seq != self._applied_seq:
            self.overwritten += 1

        self._seq += 1
        self._deposit = Deposit(
            cmd=cmd,
            at=self._t,
            seq=self._seq,
            # The anchor is read once, now. See Deposit for why re-reading it on
            # every application would turn a heading offset into a turn rate.
            yaw_target=_wrap(self._yaw + cmd.payload.dyaw),
        )

    def step(self, dt: float) -> None:
        """Apply the standing intent, or the neutral, and integrate for ``dt``.

        ``dt`` is rejected rather than repaired. A non-finite or non-positive step
        has no integration at all, and one larger than ``max_step`` is a loop that
        lost its cadence; silently clamping it would hide the loss and report a
        trajectory nobody flew.
        """
        try:
            dt = float(dt)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"AttitudeSim.step needs a real dt, got {dt!r}") from exc
        if not math.isfinite(dt) or dt <= 0.0:
            raise ValueError(f"AttitudeSim.step needs a finite positive dt, got {dt!r}")
        if dt > self._max_step:
            raise ValueError(
                f"AttitudeSim.step was handed {dt!r} s, above max_step "
                f"{self._max_step!r} s: an explicit integrator does not survive it"
            )

        payload, kind = self._fly(dt)

        self._t += dt
        self.steps += 1
        self.last_flown = payload
        self.last_applied = kind
        self.applied[kind] += 1

    # --- the Truth protocol ------------------------------------------------

    def true_state(self, agent: AgentId) -> SelfState:
        """The integrator's state, free of any estimation error.

        Identical to what :meth:`observe` reports **today**, because nothing in this
        backend degrades the estimate. The two names exist apart so that the day a
        degradation is inserted, every consumer is already reading the one it is
        entitled to, and no code has to be found and changed at that moment. Nothing
        may rely on them being equal.
        """
        self._require_known(agent)
        return self._state()

    # --- instrumentation ---------------------------------------------------

    @property
    def attitude(self) -> tuple[float, float]:
        """``(roll, pitch)`` actually flown, in radians. Internal model state.

        Not part of :class:`argos.core.World` and not something a real vehicle
        reports through this interface; :class:`argos.core.SelfState` carries no
        attitude, which is also why the safety layer refuses to guard body rates.
        """
        return (self._roll, self._pitch)

    @property
    def standing(self) -> Deposit | None:
        """The intent that would be applied by the next step, if any."""
        return self._deposit

    # --- internals ---------------------------------------------------------

    def _require_known(self, agent: AgentId) -> None:
        if agent != self._agent:
            raise KeyError(f"no agent {agent!r} in this world")

    def _refuse(self, kind: Refusal, detail: str) -> None:
        self.refusals[kind] += 1
        self.last_refusal = detail

    def _state(self) -> SelfState:
        """The current state, with arrays the caller cannot write through.

        :class:`argos.core.SelfState` is frozen but its arrays are not, so a
        consumer holding one could reach into this integrator and move the aircraft.
        This is the point at which that had to be settled, because it is the first
        code that hands states out: **every state leaves as a copy, and the copy is
        read-only.** The copy costs a few hundred nanoseconds and removes a class of
        bug that would otherwise be found by watching a simulation misbehave.
        """
        pos = self._pos.copy()
        vel = self._vel.copy()
        pos.flags.writeable = False
        vel.flags.writeable = False
        return SelfState(
            t=self._t,
            pos=pos,
            vel=vel,
            yaw=self._yaw,
            armed=self._armed,
            alive=True,
        )

    def _fly(self, dt: float) -> tuple[AttitudeCmd, Applied]:
        """Integrate one step, splitting it at the deadline if the deadline is inside.

        Returns the payload being flown at the *end* of the step and what happened.
        The neutral is level, heading held, altitude held -- the same one the safety
        layer emits, imported rather than restated so there is one of them.
        """
        deposit = self._deposit

        if deposit is None:
            self._integrate(NEUTRAL_ATTITUDE, self._yaw, dt)
            return NEUTRAL_ATTITUDE, Applied.IDLE

        remaining = deposit.at + self._command_lifetime - self._t

        if remaining >= dt:
            kind = Applied.FRESH if deposit.seq != self._applied_seq else Applied.HELD
            self._applied_seq = deposit.seq
            self._integrate(deposit.cmd.payload, deposit.yaw_target, dt)
            return deposit.cmd.payload, kind

        # The deadline falls inside this step, or has already passed. Fly the intent
        # up to the instant it expires and the neutral for what is left, so the
        # announced bound is the real one instead of the real one minus a step.
        flown_for = max(0.0, remaining)
        if flown_for > 0.0:
            self._applied_seq = deposit.seq
            self._integrate(deposit.cmd.payload, deposit.yaw_target, flown_for)

        # Dropped, not skipped. Leaving it in place would let a later step find it
        # and fly it, which is exactly the surprise this bound exists to remove: an
        # intent that has stopped acting can never start again.
        self._deposit = None
        self.expired_at = self._t + flown_for

        self._integrate(NEUTRAL_ATTITUDE, self._yaw, dt - flown_for)
        return NEUTRAL_ATTITUDE, Applied.EXPIRED

    def _integrate(self, payload: AttitudeCmd, yaw_target: float, dt: float) -> None:
        """One explicit step: attitude, then heading, then translation.

        Semi-implicit Euler -- velocity is updated first and position with the new
        velocity -- which is stable over the step sizes this backend accepts, where
        the fully explicit form drifts outward.
        """
        v = self._vehicle

        # Attitude follows the command through a first-order lag. Written as a
        # discrete blend rather than tau * dx/dt so the coefficient stays inside
        # [0, 1] for every accepted dt, including a tau of zero.
        alpha = 1.0 if v.tau_attitude <= 0.0 else dt / (v.tau_attitude + dt)
        self._roll += alpha * (payload.roll - self._roll)
        self._pitch += alpha * (payload.pitch - self._pitch)

        # Heading slews toward the absolute heading the deposit resolved to, at a
        # bounded rate, over the shortest way round.
        error = _wrap(yaw_target - self._yaw)
        limit = v.yaw_rate * dt
        self._yaw = _wrap(self._yaw + _clamp(error, -limit, limit))

        # A quadrotor accelerates by tilting: it points part of its thrust sideways.
        # Nose up is a positive pitch, so advancing is a negative one.
        roll = _clamp(self._roll, -v.model_tilt_limit, v.model_tilt_limit)
        pitch = _clamp(self._pitch, -v.model_tilt_limit, v.model_tilt_limit)
        forward = -GRAVITY * math.tan(pitch)
        right = GRAVITY * math.tan(roll)

        # Body to ENU. Heading is a compass heading in the ENU frame of
        # SelfState: zero points along +y (North) and grows turning right, so
        # forward is (sin yaw, cos yaw) and right is (cos yaw, -sin yaw).
        sin_yaw, cos_yaw = math.sin(self._yaw), math.cos(self._yaw)
        ax = forward * sin_yaw + right * cos_yaw - v.drag * self._vel[0]
        ay = forward * cos_yaw - right * sin_yaw - v.drag * self._vel[1]

        # Thrust is a climb-rate request, per AttitudeCmd: the stand-in autopilot
        # closes the altitude loop, which is what keeps an ATTITUDE command usable
        # with no vertical estimate of any kind.
        # No drag term on the vertical axis: the altitude loop owns that axis and
        # already settles it, so a second first-order term would be the same lag
        # counted twice rather than a separate physical effect.
        climb = (_clamp(payload.thrust, 0.0, 1.0) - 0.5) * 2.0 * v.max_climb_rate
        beta = 1.0 if v.tau_climb <= 0.0 else dt / (v.tau_climb + dt)

        self._vel[0] += ax * dt
        self._vel[1] += ay * dt
        self._vel[2] += beta * (climb - self._vel[2])
        self._pos += self._vel * dt

        # A floor, and nothing more: no contact dynamics, no ground effect, no
        # landing. It exists so a descent below the takeoff height reads as an
        # aircraft on the ground instead of one flying underground.
        if self._pos[2] < 0.0:
            self._pos[2] = 0.0
            if self._vel[2] < 0.0:
                self._vel[2] = 0.0
