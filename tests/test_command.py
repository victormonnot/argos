"""Properties of the command types that must never regress.

These are not tests that the dataclasses store what they were given. They check
the two design decisions the module was built around, and the safety-relevant
defaults, because those are what a later refactor would quietly undo.
"""
from __future__ import annotations

import dataclasses

import pytest

from argos.core import (
    AccelCmd,
    AttitudeCmd,
    Command,
    CommandSource,
    CommandSpace,
    CtbrCmd,
    NEUTRAL_ATTITUDE,
    VelocityCmd,
)

PAYLOADS = [
    (VelocityCmd(), CommandSpace.VELOCITY),
    (AccelCmd(), CommandSpace.ACCEL),
    (AttitudeCmd(), CommandSpace.ATTITUDE),
    (CtbrCmd(roll_rate=0.0, pitch_rate=0.0, yaw_rate=0.0, thrust=0.4), CommandSpace.CTBR),
]


@pytest.mark.parametrize("payload, expected", PAYLOADS, ids=lambda v: getattr(v, "value", ""))
def test_space_is_read_from_the_payload(payload, expected) -> None:
    """A command's space comes from what it carries, so it cannot lie about it.

    The earlier design stored the space beside a typed payload, which made
    ``Command(space=VELOCITY, payload=CtbrCmd(...))`` constructible. Such a command
    would be clipped against velocity limits while carrying body rates.
    """
    assert Command(payload=payload).space is expected


def test_space_cannot_be_set_independently_of_the_payload() -> None:
    """There is no way to construct a command whose space contradicts its payload."""
    with pytest.raises(TypeError):
        Command(payload=VelocityCmd(), space=CommandSpace.CTBR)  # type: ignore[call-arg]


def test_every_command_space_has_exactly_one_payload_type() -> None:
    """Adding a space without a payload, or the reverse, fails here.

    The two enumerations have to stay in step: a space with no payload cannot be
    commanded, and a payload with no space cannot be dispatched by the safety
    filter.
    """
    covered = {payload.space for payload, _ in PAYLOADS}
    assert covered == set(CommandSpace)


@pytest.mark.parametrize("payload, _", PAYLOADS, ids=lambda v: getattr(v, "value", ""))
def test_replace_does_not_mutate_the_original(payload, _) -> None:
    """Clipping produces a new command; the original stays intact.

    The unclipped command is the record of what was asked for, and the difference
    between the two is the record of the safety filter intervening. Mutating in
    place would erase exactly the events worth reviewing after a flight.
    """
    original = Command(payload=payload, source=CommandSource.TRACK, t=1.5)
    first_field = next(iter(dataclasses.fields(payload))).name

    clipped = original.replace(**{first_field: 0.123})

    assert getattr(clipped.payload, first_field) == 0.123
    assert clipped.payload is not original.payload
    assert original.payload == payload
    assert clipped.source is original.source and clipped.t == original.t


def test_attitude_neutral_holds_altitude() -> None:
    """A default AttitudeCmd is level, heading held, altitude held.

    0.5 thrust is what the autopilot reads as "hold this altitude" while closing
    the loop on the barometer. If this default ever drifts, every path that emits a
    neutral command starts commanding a climb or a descent.
    """
    neutral = AttitudeCmd()
    assert (neutral.roll, neutral.pitch, neutral.dyaw) == (0.0, 0.0, 0.0)
    assert neutral.thrust == 0.5


def test_ctbr_requires_every_field() -> None:
    """CtbrCmd has no defaults, so it can never be half-specified.

    Thrust here is a raw collective with no neutral value: a default of zero would
    make ``CtbrCmd()`` mean "cut the motors" while reading like "nothing in
    particular".
    """
    with pytest.raises(TypeError):
        CtbrCmd()  # type: ignore[call-arg]


def test_the_neutral_is_a_payload_not_a_ready_made_command() -> None:
    """NEUTRAL_ATTITUDE is a payload, so it carries no timestamp to go stale.

    A module-level Command would hold one fixed `t` for the life of the process. It
    would therefore be either permanently stale or permanently exempt from the
    freshness rule, and a standing exemption on the command emitted most often is
    the last place to want one. Callers stamp it; the gate does not rejuvenate it.
    """
    assert isinstance(NEUTRAL_ATTITUDE, AttitudeCmd)
    assert not isinstance(NEUTRAL_ATTITUDE, Command)
    assert NEUTRAL_ATTITUDE.space is CommandSpace.ATTITUDE
    assert NEUTRAL_ATTITUDE == AttitudeCmd()


def test_a_bad_source_is_refused_at_construction() -> None:
    """`source` is audit data, so a typo has to be an error and not a string.

    A plain annotation is not a runtime check: `Command(source="typo")` used to be
    built without complaint and travel through the gate, putting a source in the
    log that no reader could map back to anything.
    """
    with pytest.raises(TypeError, match="CommandSource"):
        Command(payload=AttitudeCmd(), source="operator")
    with pytest.raises(TypeError, match="CommandSource"):
        Command(payload=AttitudeCmd(), source=None)


def test_yaw_semantics_differ_between_attitude_and_rate_spaces() -> None:
    """Attitude carries an offset from the measured heading; the others carry a rate.

    Without a reliable compass the estimated heading drifts, so a commanded
    absolute heading would slowly diverge from the real one. An offset re-anchored
    on every command cannot. Collapsing these two onto one field would erase that
    distinction, which is a deliberate design decision and not an inconsistency.
    """
    assert hasattr(AttitudeCmd(), "dyaw")
    assert not hasattr(AttitudeCmd(), "yaw_rate")
    for payload in (VelocityCmd(), AccelCmd(), CtbrCmd(0.0, 0.0, 0.0, 0.4)):
        assert hasattr(payload, "yaw_rate")
        assert not hasattr(payload, "dyaw")
