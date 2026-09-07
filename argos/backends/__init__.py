"""The backends: everything that turns a command into motion, behind one interface.

**No control layer imports a simulator.** Guidance, safety and perception speak
:class:`argos.core.World` and nothing else, so moving from a point-mass simulation
to an airframe carrying its own computer changes a backend and not a control layer.
If a port needs a control layer touched, the interface is leaking and the interface
is what gets fixed.

A backend is also where ground truth legitimately lives: in simulation the
authoritative answer is the integrator's own state, and on a bench it is an external
reference. ``tests/test_core_isolation.py`` allows this package to read
:mod:`argos.core.truth` and forbids every control layer from doing so.

:class:`argos.backends.AttitudeSim` is the first one, and the place the deposit
lifecycle left open in :meth:`argos.core.World.command` is answered as behaviour.
"""
from .attitude_sim import GRAVITY, Applied, AttitudeSim, Deposit, Refusal, Vehicle

__all__ = [
    "Applied",
    "AttitudeSim",
    "Deposit",
    "GRAVITY",
    "Refusal",
    "Vehicle",
]
