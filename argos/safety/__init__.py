"""The safety layer: the only way a command reaches a world.

Three modules, and the split is the point. :mod:`argos.safety.validate` decides
whether an input is usable at all; :mod:`argos.safety.envelope` states what a
command is allowed to be; :mod:`argos.safety.gate` is the single door every command
goes through. None of them computes a command, so none can be wrong in the way a
control law can be wrong.

Where each kind of error is caught, stated once: a **type** error is a programmer
error and raises at construction in :mod:`argos.core`; a **data** error is a
runtime condition and is refused here, at the boundary that can respond safely.
"""
from .envelope import DEG, Envelope, clamp, clip
from .gate import CommandGate, GateResult, Intervention, is_flying
from .validate import (NUMERIC_KINDS, SUPPORTED_DIMS, check_clock,
                       check_detection, check_payload, check_scalar,
                       check_state, check_target)

__all__ = [
    "CommandGate",
    "DEG",
    "Envelope",
    "GateResult",
    "Intervention",
    "NUMERIC_KINDS",
    "SUPPORTED_DIMS",
    "check_clock",
    "check_detection",
    "check_payload",
    "check_scalar",
    "check_state",
    "check_target",
    "clamp",
    "clip",
    "is_flying",
]
