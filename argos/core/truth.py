"""Ground truth: an authoritative external reference, for instrumentation only.

This module is deliberately **not** re-exported by :mod:`argos.core`, so importing
``World`` never drags it in and reaching for it is always an explicit act.

It is not a simulation-only idea, which is why it lives in the shared contract
rather than under a backend. A simulator is one implementation. An instrumented
bench is another, and the bench is the one that matters: the safety filter and the
standoff distance have to be checked against a real measurement before the
aircraft is ever flown toward a person, and that measurement needs a name in the
contract. Filing this under a simulator package would leave nothing to call the
tape measure, and would force the measurement code to depend on the very backend
it is supposed to be able to replace.

The degradation harness is the reason this exists at all: it measures the gap
between what the system believes and what is actually true, and there is no way to
express that without naming both.

**Any control layer that calls this is cheating.** Separation alone does not stop
that, so ``tests/test_core_isolation.py`` enumerates the packages allowed to import
this module and fails the build for every other one. A layer that cheats does not
merely become visible on review; it stops being mergeable.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .observation import AgentId, SelfState


@runtime_checkable
class Truth(Protocol):
    """An authority that can be asked what is actually the case.

    In simulation this reads the integrator's state. On a bench it comes from an
    external reference more accurate than anything the aircraft carries: a surveyed
    marker, a measured distance, a motion capture rig.
    """

    def true_state(self, agent: AgentId) -> SelfState:
        """The agent's actual state, free of the estimation error being measured."""
        ...
