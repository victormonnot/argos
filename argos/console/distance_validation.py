"""Console-owned validation of experimental yaw and apparent-distance demands."""
from copy import copy
from dataclasses import asdict

from argos.backends.distance_source import DistanceDemand
from argos.backends.vision_bench_source import PreviewError
from argos.backends.yaw_stream_source import YawValidator
from argos.guidance.apparent_distance import ApparentDistanceLaw


class DistanceValidator:
    def __init__(self):
        self.yaw = YawValidator()
        self.law = ApparentDistanceLaw()
        self.mode = "Y"
        self.key = None

    def validate(self, state, started, finished, *, explicit_selection=False,
                 mode=None, observation=None):
        yaw = copy(self.yaw)
        demand = yaw.validate(state, started, finished, explicit_selection=explicit_selection)
        if explicit_selection:
            if mode not in ("Y", "D"):
                raise PreviewError("Explicit distance mode is required")
            self.mode = mode
            self.law = ApparentDistanceLaw()
            self.key = demand.selection_key
        elif self.key != demand.selection_key:
            self.law = ApparentDistanceLaw()
            self.key = demand.selection_key
        self.yaw = yaw
        if self.mode == "Y":
            return DistanceDemand(**asdict(demand))
        box, receipt = None, None
        if demand.valid and observation is not None:
            preview = state["yaw_preview"]
            if (observation["run_id"] == preview["run_id"]
                    and observation["video_id"] == preview["video_id"]
                    and observation["sequence"] == demand.frame_sequence
                    and observation["received_at"] == preview["frame_received_at"]):
                matches = [d for d in observation["detections"]
                           if d["track_id"] == preview["target_id"] and d["confidence"] >= .65]
                if len(matches) == 1:
                    box, receipt = matches[0]["box"], observation["received_at"]
        if explicit_selection:
            if box is None:
                raise PreviewError("Distance reference needs a recent strong selected person")
            try:
                self.law.start(box, receipt)
            except ValueError as exc:
                raise PreviewError(str(exc)) from exc
        elif box is None or self.law.reference is None:
            self.law.pause()
        else:
            try:
                self.law.update(box, receipt)
            except ValueError:
                self.law.pause("invalid_box")
        active = demand.valid and self.law.valid
        return DistanceDemand(**asdict(demand), pitch=round(self.law.value * 1024) if active else 0,
                              pitch_valid=active, mode="D", distance_reason=self.law.reason,
                              reference_height=self.law.reference)
