"""The harness: measuring a run rather than flying one.

Nothing here produces a command. It is the package that says what happened -- how much
of the channel survived, how long the loop went quiet, how old the information behind
a command was -- and it is therefore the package allowed to read
:mod:`argos.core.truth`, because measuring the gap between what a system believes and
what is actually the case needs both.

:mod:`argos.harness.link` is the first piece and depends on nothing at all: it is
handed numbers and returns numbers, so it can be pointed at MAVLink, at a CRSF link
that shares none of its framing, or at a bench with no radio in it.
"""
from .link import (
    LatencySummary,
    LinkSnapshot,
    LinkStats,
    SeqCounts,
    SeqEvent,
    SeqPolicy,
    SequenceAudit,
)

__all__ = [
    "LatencySummary",
    "LinkSnapshot",
    "LinkStats",
    "SeqCounts",
    "SeqEvent",
    "SeqPolicy",
    "SequenceAudit",
]
