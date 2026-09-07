"""The single exit toward the vehicle.

**No command reaches the world except through** :meth:`CommandGate.submit`. The
guidance law, the operator, and later a learned policy all go through the same
door, and the world is held privately by the gate so nothing else holds a reference
to call. That is a property of this package's structure, not of Python: a caller
that obtains the world some other way is outside what this module can see.

This replaces an arrangement where the proximity guard was an ``if`` *inside* the
tracking branch. The manual flight branch did not cross it, so the check covered
half the paths: an operator could push the aircraft into the very target the
autonomy was refusing to approach. A property that depends on which branch the code
took is not a property.

It is also the seam a reciprocal collision-avoidance filter plugs into for
multi-agent flight: something that takes a desired command and returns a safe one
is the same shape whether the hazard is a person or another aircraft.

**The gate fails closed.** Whenever an input is unusable, stale, in the wrong
space, or in a space whose guard does not exist, it deposits nothing and reports
why. Refusing to fly is recoverable; flying unguarded is not.

**What "refused" does and does not mean.** A refusal here means this gate deposited
nothing on this call. It does **not** cancel an intent deposited earlier, and it is
not a stop: the vehicle keeps flying whatever it was last given, for as long as its
backend keeps that intent alive. Cancellation is not something this gate could do
honestly anyway -- a setpoint already sent over a link cannot be recalled, and
depositing a neutral instead of nothing would make the gate emit a command on
exactly the paths where it has decided it cannot trust its inputs.

So a loop that has been refused is not finished. If the state is usable it should
follow with :meth:`hold`, which says "nothing in particular" explicitly rather than
by falling silent. How long silence is tolerated before the vehicle stops obeying
the last intent is the backend's answer, stated in :meth:`argos.core.World.command`
as something every backend must define and exercise.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from argos.core import (
    AccelCmd,
    AgentId,
    AttitudeCmd,
    Command,
    CommandSource,
    CommandSpace,
    NEUTRAL_ATTITUDE,
    SelfState,
    TargetView,
    VelocityCmd,
    World,
)

from .envelope import Envelope, clip
from .validate import check_clock, check_payload, check_state, check_target


class Intervention(Enum):
    """Why the gate altered or withheld a command.

    Audit data, like :class:`argos.core.CommandSource`. This is what gets counted
    and grepped after a flight to answer "did the safety layer act, and on what",
    so the set is closed and each member means one specific thing.
    """

    # Withheld: nothing was deposited.
    INVALID_CLOCK = "invalid_clock"
    INVALID_COMMAND = "invalid_command"
    INVALID_STATE = "invalid_state"
    INVALID_TARGET = "invalid_target"
    FUTURE_TIMESTAMP = "future_timestamp"
    STALE_COMMAND = "stale_command"
    STALE_STATE = "stale_state"
    STALE_TARGET = "stale_target"
    NOT_FLYING = "not_flying"
    UNSUPPORTED_SPACE = "unsupported_space"
    NO_GUARD = "no_guard"

    # Deposited, but changed on the way through.
    CLIPPED = "clipped"
    PROXIMITY = "proximity"


@dataclass(frozen=True)
class GateResult:
    """What the gate actually did. Returned on every call, never thrown away."""

    sent: bool
    """Whether this gate handed the command to the world on this call.

    Not an acknowledgement, and it cannot be one: :meth:`argos.core.World.command`
    returns nothing, deliberately, because a setpoint on a real link is not
    acknowledged either. What it does mean is that the command passed every check
    here and was deposited into a world that has agreed it knows this agent -- the
    gate refuses to be built against a world that does not, so ``sent=True`` can no
    longer stand over a command the backend threw away for that reason. A backend may
    still decline for reasons no caller can see; its own counters are the other half
    of the truth.
    """

    cmd: Command                              # deposited, or the input if withheld
    interventions: tuple[Intervention, ...] = ()
    detail: str = ""                          # context for a log line

    def __bool__(self) -> bool:
        return self.sent


def is_flying(state: SelfState, env: Envelope) -> bool:
    """Whether the aircraft counts as airborne.

    A **measured** fact rather than a flag someone set on takeoff: motors armed
    *and* above a height threshold. A flag can survive the event it describes; if a
    script sets ``flying = True`` and the aircraft never left the ground, every
    check downstream is reasoning about a flight that is not happening.

    Height is ``pos[2]``, metres above the takeoff point, positive upward, per
    :class:`argos.core.SelfState`. Not height above the ground: over a slope the
    two differ, and this function does not know the terrain.

    In two dimensions there is no altitude to test, so arming is the whole answer.
    That branch is only reachable in a genuinely 2-D world: the gate refuses a state
    whose dimension disagrees with its world before calling this, so a missing
    vertical coordinate can no longer skip the height check in a 3-D world.
    """
    if not state.armed:
        return False
    if len(state.pos) < 3:
        return True
    return float(state.pos[2]) > env.min_flying_alt


class CommandGate:
    """The only object in this package that calls :meth:`argos.core.World.command`."""

    def __init__(
        self,
        world: World,
        agent: AgentId = 0,
        envelope: Envelope | None = None,
    ) -> None:
        """``agent`` must be one this world knows, and is checked here.

        A gate pointed at an agent its world does not have would pass every check
        below, deposit into a backend that quietly declines the unknown id, and
        report ``sent=True`` over a command nothing ever received. That is an
        assembly mistake, not a runtime condition, so it raises at construction where
        the traceback points at the line that got it wrong rather than at a flight
        that did not happen.

        This is a static consistency check and not an acknowledgement protocol:
        :meth:`argos.core.World.command` still returns nothing. What it removes is the
        one case where the gate could be wrong about who it is talking to before a
        single command is sent.

        It says nothing about an agent that *leaves* mid-run. Attrition shortens
        :meth:`argos.core.World.agents`, and a state whose ``alive`` is cleared is
        already refused by :func:`argos.safety.check_state`; what a shrinking roster
        means for a gate already built belongs to the multi-agent backend that will
        define it.
        """
        known = tuple(world.agents())
        if agent not in known:
            raise ValueError(
                f"CommandGate is set to agent {agent!r} but its world knows {known}: "
                f"every command would be declined by the world and reported as sent"
            )

        self._world = world
        self._agent = agent
        self.env = envelope or Envelope()
        self.last_sent: float | None = None
        """World-clock time of the last deposit, or ``None`` if nothing was ever
        deposited. On the same clock as everything else the gate compares."""

        self.last = GateResult(
            False,
            Command(payload=NEUTRAL_ATTITUDE, source=CommandSource.IDLE),
            (Intervention.NOT_FLYING,),
            "never sent",
        )
        self.interventions = 0
        """How many commands the gate has altered or withheld. Instrumentation: a
        gate that never intervenes has either perfect callers or a broken check,
        and the count is what tells the two apart."""

    def submit(
        self,
        cmd: Command,
        state: SelfState,
        target: TargetView | None = None,
    ) -> GateResult:
        """The only path to the world. Returns what was really deposited.

        ``target=None`` means no target is designated, which is a legitimate state:
        there is nothing to be close to and the proximity guard does not apply. A
        target that *is* supplied but is unusable is refused instead, because
        letting it fall through would silently turn a broken perception feed into
        an absence of guard.
        """
        # --- the clock everything else is measured against ----------------
        # Read and checked before any comparison uses it. A NaN clock makes every
        # age comparison false at once, which silently disables every limit below
        # rather than tripping any one of them.
        now = self._world.time()
        why = check_clock(now)
        if why:
            return self._withhold(cmd, Intervention.INVALID_CLOCK, why)
        now = float(now)
        acted: list[Intervention] = []

        # --- inputs are usable at all ------------------------------------
        why = check_payload(cmd)
        if why:
            return self._withhold(cmd, Intervention.INVALID_COMMAND, why)

        why = check_state(state, dim=self._world.dim)
        if why:
            return self._withhold(cmd, Intervention.INVALID_STATE, why)

        if target is not None:
            why = check_target(target)
            if why:
                return self._withhold(cmd, Intervention.INVALID_TARGET, why)

        # --- timestamps belong to this clock ------------------------------
        # A stamp ahead of the world clock is a second time base leaking in, not a
        # very fresh input. Left unchecked it reads as newly produced, and on a
        # target it cancels the declared detection age through a negative elapsed
        # term.
        #
        # With the default `max_clock_skew` of zero this also makes every age below
        # non-negative, since `t <= now`. That argument does NOT survive a positive
        # tolerance: a stamp up to `max_clock_skew` ahead would shrink a declared
        # age by that much, so a target whose own `age` already exceeds the limit
        # could still be admitted. The elapsed term is therefore floored at zero
        # below, which costs nothing at zero tolerance and keeps the property true
        # if the tolerance is ever widened.
        for label, stamp in (("command", cmd.t), ("state", state.t),
                             ("target", target.t if target is not None else None)):
            if stamp is not None and stamp > now + self.env.max_clock_skew:
                return self._withhold(
                    cmd,
                    Intervention.FUTURE_TIMESTAMP,
                    f"{label} is stamped {stamp - now:.3f} s ahead of the world clock, "
                    f"tolerance {self.env.max_clock_skew:.3f} s",
                )

        # --- inputs are recent enough ------------------------------------
        if cmd.t is None:
            return self._withhold(
                cmd, Intervention.STALE_COMMAND, "command carries no timestamp"
            )
        command_age = now - cmd.t
        if command_age > self.env.max_command_age:
            return self._withhold(
                cmd,
                Intervention.STALE_COMMAND,
                f"command is {command_age:.3f} s old, limit {self.env.max_command_age:.3f} s",
            )

        state_age = now - state.t
        if state_age > self.env.max_state_age:
            return self._withhold(
                cmd,
                Intervention.STALE_STATE,
                f"state is {state_age:.3f} s old, limit {self.env.max_state_age:.3f} s",
            )

        if target is not None and target.has:
            if target.t is None:
                return self._withhold(
                    cmd, Intervention.STALE_TARGET, "target view carries no timestamp"
                )
            # A stored age does not grow on its own: add the time elapsed since the
            # view was computed, or a perception loop that stopped would keep
            # reporting whatever age it last wrote. The elapsed term is floored at
            # zero so that a stamp tolerated as clock skew can never make a
            # detection look younger than the producer said it was.
            effective_age = target.age + max(0.0, now - target.t)
            if effective_age > self.env.max_target_age:
                return self._withhold(
                    cmd,
                    Intervention.STALE_TARGET,
                    f"target last seen {effective_age:.3f} s ago, "
                    f"limit {self.env.max_target_age:.3f} s",
                )

        # --- the aircraft and the world can take this command -------------
        if not is_flying(state, self.env):
            return self._withhold(cmd, Intervention.NOT_FLYING, "aircraft is not airborne")

        if cmd.space is not self._world.command_space():
            return self._withhold(
                cmd,
                Intervention.UNSUPPORTED_SPACE,
                f"world speaks {self._world.command_space().value}, got {cmd.space.value}",
            )

        if cmd.space is CommandSpace.CTBR:
            # Fails closed, deliberately. The proximity guard is expressed as
            # "remove the forward component", and a body rate is a rotation, not a
            # motion: zeroing a pitch rate holds whatever forward pitch the
            # aircraft already has. Guarding this space needs the current attitude,
            # which SelfState does not carry. Until it does, refusing is the only
            # honest answer.
            return self._withhold(
                cmd, Intervention.NO_GUARD, "no proximity guard exists for body rates"
            )

        # --- bring it inside the envelope ---------------------------------
        safe = clip(cmd.payload, self.env)
        if safe != cmd.payload:
            acted.append(Intervention.CLIPPED)

        detail = ""
        if target is not None and target.has and target.size >= self.env.size_stop:
            guarded = _remove_forward(safe)
            if guarded != safe:
                safe = guarded
                acted.append(Intervention.PROXIMITY)
                detail = f"apparent size {target.size:.3f} >= {self.env.size_stop:.3f}"

        out = Command(payload=safe, source=cmd.source, t=cmd.t)
        self._world.command(self._agent, out)
        self.last_sent = now
        self.interventions += bool(acted)
        self.last = GateResult(True, out, tuple(acted), detail)
        return self.last

    def hold(self, state: SelfState) -> GateResult:
        """Emit the neutral attitude, stamped now.

        Something has to be emitted continuously so a guided mode's setpoint
        timeout never expires by accident. The autopilot taking control back is
        correct behaviour, but it has to be a decision rather than a consequence of
        the loop being briefly busy.

        **ATTITUDE only, for now.** A world that speaks velocity or acceleration
        refuses this with ``UNSUPPORTED_SPACE``, which is the honest outcome: the
        neutral element of those spaces is a different command, and inventing one
        before a backend exists to fly it would be a guess. The refusal is tested.

        This holds attitude and asks the altitude loop to hold. It does **not**
        hold horizontal position: with no position estimate the aircraft drifts
        with the wind while flying a perfectly neutral attitude.
        """
        return self.submit(
            Command(
                payload=NEUTRAL_ATTITUDE,
                source=CommandSource.IDLE,
                t=self._world.time(),
            ),
            state,
        )

    def _withhold(self, cmd: Command, why: Intervention, detail: str) -> GateResult:
        self.interventions += 1
        self.last = GateResult(False, cmd, (why,), detail)
        return self.last


def _remove_forward(payload):
    """The proximity guard: strip the component that closes the distance.

    Only that component is removed. Braking, backing off and turning stay
    available, because the aircraft still has to be flyable while the guard is
    active: a guard that froze every axis would trade a possible collision for an
    aircraft nobody can recover.

    Forward is a different field and a different sign in each space, which is why
    this cannot be a single clamp:

        ATTITUDE   nose down is a NEGATIVE pitch, so forward is pitch < 0
        VELOCITY   forward is vx > 0
        ACCEL      forward is ax > 0

    This bounds a command. It does not stop motion already under way, and it is not
    a statement about distance; see :attr:`argos.safety.Envelope.size_stop`.
    """
    if isinstance(payload, AttitudeCmd):
        if payload.pitch < 0.0:
            return AttitudeCmd(payload.roll, 0.0, payload.dyaw, payload.thrust)
        return payload
    if isinstance(payload, VelocityCmd):
        if payload.vx > 0.0:
            return VelocityCmd(0.0, payload.vy, payload.vz, payload.yaw_rate)
        return payload
    if isinstance(payload, AccelCmd):
        if payload.ax > 0.0:
            return AccelCmd(0.0, payload.ay, payload.az, payload.yaw_rate)
        return payload
    return payload
