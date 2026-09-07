"""Admission checks: is this input usable at all.

The division of labour, stated once so no other module has to guess:

*Type errors* are programmer errors and are caught at construction, in
:mod:`argos.core`, where the traceback points at the offending line. A command
whose source is a string never gets built.

*Data errors* are runtime conditions and are caught here, at the boundary, because
this is the only place that can respond safely. A non-finite number arriving from a
detector, a state older than the loop period, an array of the wrong dtype from a
misconfigured driver -- none of these are bugs in the caller, they are things that
happen, and the answer is to refuse and say so rather than to raise.

**Every function here returns a reason string rather than raising, for every input
these types can hold.** That is the whole point: a caller in a control loop must
receive a refusal it can act on, not an exception it has to catch. An earlier
version reached ``numpy.isfinite`` with an array of strings and let a ``TypeError``
escape into the loop, which is the same failure as the ones being checked for, one
level up; another let ``float()`` raise ``OverflowError`` on an integer too large
to convert.

The claim is bounded on purpose, because an unbounded one would be false: a hostile
object whose ``__float__`` raises something unforeseen is not covered, and catching
everything to pretend otherwise would swallow real bugs in this module. What is
covered is every value a dataclass field or a NumPy array can actually carry.

The check that motivates the module is finiteness. ``NaN`` compares false against
everything, so it survives every bound written as a comparison and it turns every
threshold test into "no". Clipping does not stop it and the proximity guard does not
see it. Both were reproduced against this package before this module existed.
"""
from __future__ import annotations

import math
import numbers
from dataclasses import fields

import numpy as np

from argos.core import Command, Detection, SelfState, TargetView

SUPPORTED_DIMS = (2, 3)
"""The state dimensions this project supports. Two for planar work, three for flight."""

NUMERIC_KINDS = frozenset("fiu")
"""NumPy dtype kinds accepted in a state array: float, signed int, unsigned int.

Everything else is refused by name rather than passed to a ufunc that would raise:
``object`` arrays (including ones holding perfectly ordinary numbers), string
arrays, and booleans, which are integers wearing a disguise and never a position.
"""


def check_scalar(value: object, label: str) -> str:
    """Empty if ``value`` is a usable real number, otherwise why it is not.

    NumPy real scalars are accepted. A producer computing a command from arrays
    naturally hands over ``np.float32``, and refusing that would be a trap for the
    caller and a lie in the message: ``np.float32(0.1)`` is perfectly finite.
    ``bool`` is excluded even though Python calls it an integer, because a boolean
    roll angle is a type confusion rather than a value.

    The two failures are reported apart. "Not a real number" and "not finite" send a
    reader to different places.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return f"{label} is not a real number: {type(value).__name__}"
    try:
        as_float = float(value)
    except (OverflowError, ValueError) as exc:
        # A Python int has no size limit, so `10**400` is a perfectly ordinary
        # `numbers.Real` that cannot become a float at all. It is far outside any
        # useful range here, but it arrives as a value rather than as a wrong type,
        # and the answer to a value is a refusal, not an exception in the loop.
        return f"{label} is outside the supported floating-point range: {exc}"
    if not math.isfinite(as_float):
        return f"{label} is not a finite number: {value!r}"
    return ""


def _array_problem(value: object, label: str) -> str:
    """Empty if ``value`` is a usable 1-D numeric array, otherwise why it is not.

    The conversion is attempted inside a narrow guard. A ragged nested sequence
    makes ``numpy.asarray`` raise, and that is a malformed input like any other, to
    be refused rather than propagated. The guard names the two exceptions the
    conversion is documented to produce instead of swallowing everything, so a real
    bug in this module still surfaces.
    """
    try:
        array = np.asarray(value)
    except (ValueError, TypeError) as exc:
        return f"{label} is not convertible to an array: {exc}"

    if array.dtype.kind not in NUMERIC_KINDS:
        return f"{label} has a non-numeric dtype: {array.dtype}"
    if array.ndim != 1 or array.shape[0] not in SUPPORTED_DIMS:
        return (
            f"{label} must be 1-D with {SUPPORTED_DIMS[0]} or {SUPPORTED_DIMS[1]} "
            f"elements, got shape {array.shape}"
        )
    if not np.all(np.isfinite(array)):
        return f"{label} contains a non-finite value: {array!r}"
    return ""


def check_clock(now: object) -> str:
    """Empty if the world's clock reading is usable, otherwise why it is not.

    Checked before anything is compared against it. A ``NaN`` clock makes every
    freshness comparison false, so an unchecked clock silently disables every age
    limit at once and lets any command through.
    """
    return check_scalar(now, "world clock")


def check_payload(cmd: Command) -> str:
    """Empty if the command's numbers are usable, otherwise why they are not."""
    for f in fields(cmd.payload):
        why = check_scalar(
            getattr(cmd.payload, f.name), f"{type(cmd.payload).__name__}.{f.name}"
        )
        if why:
            return why
    if cmd.t is not None:
        return check_scalar(cmd.t, "command timestamp")
    return ""


def check_state(state: SelfState, dim: int | None = None) -> str:
    """Empty if the state is usable, otherwise why it is not.

    ``dim``, when given, is the dimension of the world this state is going to be
    acted on in, and the two must agree. Without that comparison a 2-D state is
    accepted by a 3-D world, and every consumer that reads ``pos[2]`` then takes a
    branch meant for planar work: a missing vertical coordinate turns into an
    authorisation in a world where height is exactly what has to be checked.

    ``alive=False`` is refused rather than treated as an ordinary state. The field
    means the agent has been removed from the run, and commanding something that is
    gone is either a bug in the caller or a stale reference; either way it must not
    reach a vehicle.
    """
    if not state.alive:
        return "agent is not alive"

    why = check_scalar(state.t, "state timestamp")
    if why:
        return why
    why = check_scalar(state.yaw, "state yaw")
    if why:
        return why

    for name in ("pos", "vel"):
        why = _array_problem(getattr(state, name), f"state {name}")
        if why:
            return why

    pos_shape = np.asarray(state.pos).shape
    vel_shape = np.asarray(state.vel).shape
    if pos_shape != vel_shape:
        return f"state pos and vel disagree on dimension: {pos_shape} vs {vel_shape}"

    if dim is not None and pos_shape[0] != dim:
        return f"state is {pos_shape[0]}-D but the world is {dim}-D"
    return ""


def check_target(target: TargetView) -> str:
    """Empty if the target view is usable, otherwise why it is not.

    Only called when a target was actually supplied. A caller passing ``None`` is
    saying "no target is designated", which is a legitimate state and not an error;
    a caller passing a target full of ``NaN`` is saying nothing at all, and letting
    that quietly turn into "no proximity guard" is the failure this check exists to
    prevent.
    """
    for name in ("error_x", "error_y", "size", "age", "confidence"):
        why = check_scalar(getattr(target, name), f"target {name}")
        if why:
            return why
    if target.size < 0.0:
        return f"target size is negative: {target.size!r}"
    if target.age < 0.0:
        return f"target age is negative: {target.age!r}"
    if target.t is not None:
        return check_scalar(target.t, "target timestamp")
    return ""


def check_detection(detection: Detection) -> str:
    """Empty if one detector output is usable, otherwise why it is not.

    A detector is a third party -- a network whose output nobody proof-reads, or a
    stub in a test -- and its numbers reach a tracker that *remembers*. That is the
    same shape as the defect the guidance law had: one unusable value entering a
    memory is not one bad frame, it is every frame afterwards until the lock is
    dropped. Checked here so "usable" means one thing across the project.

    The box is normalised to the image, so the fractions have to be fractions. A
    detector reporting a box wider than the frame has either changed convention or
    broken, and neither is something to associate a target with.
    """
    for name in ("cx", "cy", "w", "h", "confidence"):
        why = check_scalar(getattr(detection, name), f"detection {name}")
        if why:
            return why
    for name in ("cx", "cy"):
        value = getattr(detection, name)
        if not 0.0 <= value <= 1.0:
            return f"detection {name} is outside the image: {value!r}"
    for name in ("w", "h"):
        value = getattr(detection, name)
        if not 0.0 <= value <= 1.0:
            return f"detection {name} is not a fraction of the image: {value!r}"
    if not 0.0 <= detection.confidence <= 1.0:
        return f"detection confidence is outside 0..1: {detection.confidence!r}"
    if not isinstance(detection.label, str):
        return f"detection label is not a string: {type(detection.label).__name__}"
    return ""
