"""The guidance layer: image error in, desired attitude out.

Two producers of commands, one type. :mod:`argos.guidance.visual` is the law;
:mod:`argos.guidance.operator` is a human. They differ in where the intention comes
from and in nothing else, which is what lets :mod:`argos.safety` treat them alike.

Neither enforces anything. Both produce a *desired* command, and the safety layer is
what decides whether it is emitted and in what form.
"""
from .operator import operator_command
from .visual import DEG, GuidanceGains, GuidanceTelemetry, SampleKind, VisualGuidance

__all__ = [
    "DEG",
    "GuidanceGains",
    "GuidanceTelemetry",
    "SampleKind",
    "VisualGuidance",
    "operator_command",
]
