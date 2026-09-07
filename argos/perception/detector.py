"""What a detector is, expressed as the little a tracker needs it to be.

Deliberately one method. Everything that makes a real detector hard -- weights,
input size, quantisation, which runtime it sits on, whether it runs on a laptop's
GPU or a companion computer's NPU -- is behind this and is a property of the
implementation, not of the layer that consumes boxes.

No implementation ships here yet. A concrete detector needs weights and an inference
runtime, and it arrives with the benchmark that says what it costs, because a
detector without a measured cost is not a component, it is a guess about one.
"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

import numpy as np

from argos.core import Detection


@runtime_checkable
class Detector(Protocol):
    """Turns one image into the objects it contains."""

    def detect(self, image: np.ndarray) -> Sequence[Detection]:
        """Every object found in this image, normalised to it.

        Normalised, so nothing downstream knows the resolution: the same numbers
        describe the same geometry whether the frame came from a simulator, a
        degraded analog downlink or a CSI sensor. Returning an empty sequence means
        "nothing found in this image", which is a result; it is not the same as the
        detector not having run, and a caller that cannot tell those apart is going
        to read a dead detector as an empty world.
        """
        ...
