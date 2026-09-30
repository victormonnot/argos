"""Isolated experimental service using existing bounded logging/lifecycle."""
from argos.backends.edgetx_distance_stream import run_stream
from .yaw_assist import YawAssistService
from .distance_source import LocalDistanceSource


class DistanceAssistService(YawAssistService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, runner=run_stream, **kwargs)

    def bind_console(self, session, loop):
        if self._thread is not None:
            raise RuntimeError("Bind the console before starting distance assistance")
        self._local_source = LocalDistanceSource(session, loop.call_soon_threadsafe, clock=self.clock)

    def snapshot(self):
        status = super().snapshot()
        sample = self._local_source.snapshot() if self._local_source is not None else None
        demand = sample.demand if sample is not None else None
        fresh = (demand is not None and not sample.error
                 and self.clock() < demand.deadline
                 and 0 <= self.clock() - sample.completed_at < 1.)
        status["distance_preview"] = dict(
            experimental=True, pitch=demand.pitch if fresh else 0,
            valid=demand.pitch_valid if fresh else False,
            reason=demand.distance_reason if demand else "waiting",
            reference_height=demand.reference_height if demand else None)
        return status
