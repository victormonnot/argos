"""Immutable experimental yaw and apparent-distance demand for radio backends."""
from dataclasses import dataclass

from .yaw_stream_source import YawDemand


@dataclass(frozen=True)
class DistanceDemand(YawDemand):
    pitch: int = 0
    pitch_valid: bool = False
    mode: str = "Y"
    distance_reason: str = "yaw_only"
    reference_height: float | None = None
