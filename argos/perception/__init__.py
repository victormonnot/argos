"""Perception: pixels in, one designated target out.

Two boundaries and one rule between them. :class:`FrameSource` is where images come
from and is the interface that changes between a ground station on an analog downlink
and a companion computer on a CSI sensor. :class:`Detector` is what finds objects.
:class:`Tracker` is what keeps one of them designated across frames.

The rule: **validity is a question about time, and it is answered when it is asked.**
Nothing here stores a boolean saying the target is still good. A camera that goes
quiet therefore expires its target by the clock, instead of freezing the last answer
it managed to produce.
"""
from .detector import Detector
from .frame import Frame, FrameSource, check_frame
from .track import Stamp, Tracker, TrackPolicy, TrackState, TrackTelemetry

__all__ = [
    "Detector",
    "Frame",
    "FrameSource",
    "check_frame",
    "Stamp",
    "TrackPolicy",
    "TrackState",
    "TrackTelemetry",
    "Tracker",
]
