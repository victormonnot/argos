"""The contract every other package speaks.

``argos.core`` holds types and protocols only: no algorithms, no input or output,
no simulator, no autopilot protocol. Everything else in the project imports it, and
it imports nothing from the project in return. That one-way rule is what keeps the
dependency graph a tree with this package at the root, and it is checked by
``tests/test_core_isolation.py`` rather than left to discipline.

:mod:`argos.core.truth` is intentionally absent from these exports. It is an
instrumentation concern, and reaching for it should be an explicit import.
"""
from .command import (
    AccelCmd,
    AttitudeCmd,
    Command,
    CommandSource,
    CommandSpace,
    CtbrCmd,
    NEUTRAL_ATTITUDE,
    Payload,
    VelocityCmd,
)
from .observation import AgentId, Neighbor, Observation, SelfState
from .target import Detection, TargetView
from .world import World

__all__ = [
    "AccelCmd",
    "AgentId",
    "AttitudeCmd",
    "Command",
    "CommandSource",
    "CommandSpace",
    "CtbrCmd",
    "Detection",
    "NEUTRAL_ATTITUDE",
    "Neighbor",
    "Observation",
    "Payload",
    "SelfState",
    "TargetView",
    "VelocityCmd",
    "World",
]
