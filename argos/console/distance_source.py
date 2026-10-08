"""Console-owned experimental distance mailbox, with no loopback HTTP or serial I/O.

Only the event-loop owner touches the session and validator. The radio thread
reads immutable samples and posts bounded selection requests back to that owner.
If the owner stalls, neither source health nor an image deadline is renewed.
"""
from copy import deepcopy
import re
import threading
import time

from argos.backends.edgetx_yaw_stream import SelectionResult, SourceSample
from argos.backends.vision_bench_source import HTTP_TIMEOUT, PreviewError, _number
from argos.console.distance_validation import DistanceValidator


class LocalDistanceSource:
    def __init__(self, session, schedule, *, clock=time.monotonic, cpu_clock=time.thread_time):
        self.session, self.schedule = session, schedule
        self.clock, self.cpu_clock = clock, cpu_clock
        self._owner = threading.get_ident()
        self._lock = threading.Lock()
        self._closed = False
        self._validator = DistanceValidator()
        self._context = None
        self._sample = SourceSample(None, clock(), True)
        self._selection_result = None
        self._request = None
        self._scheduled = False
        self._metrics = dict(mode="in_process", count=0, errors=0, selections=0,
                             wall_ms_total=0., wall_ms_max=0., cpu_ms_total=0.,
                             cpu_ms_max=0., last_wall_ms=0., last_cpu_ms=0., last_error=None)

    def start(self):
        """The console owns publication; there is no polling thread to start."""

    def snapshot(self):
        with self._lock:
            return self._sample

    def metrics(self):
        with self._lock:
            return dict(self._metrics)

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("Yaw source must be published by the console owner")

    def publish(self, request=None):
        """Publish after a console tick, including loss/staleness without new frames."""
        self._check_owner()
        with self._lock:
            if self._closed:
                return
        started, cpu_started = self.clock(), self.cpu_clock()
        token = None if request is None else request[0]
        error = None
        try:
            if self.session._closed or not self.session._started:
                raise PreviewError("Console session is stopped")
            if request is None:
                state = self.session.yaw_assist_state(refresh=False)
            else:
                token, context, requested_at, mode = request
                if not 0 <= started - requested_at < HTTP_TIMEOUT:
                    raise PreviewError("Radio selection waited too long for the console")
                if context is None:
                    raise PreviewError("No observed camera context for radio selection")
                response = self.session.yaw_assist_select(dict(
                    request_id=token, run_id=context[0], video_id=context[1]))
                state = response["state"]
                if (response["request_id"] != token
                        or (state["run_id"], state["video"]["source_id"]) != context):
                    raise PreviewError("Camera changed during radio target selection")
            finished = self.clock()
            validator = deepcopy(self._validator)
            candidate = self.session.vision.frame(self.session) if self.session.vision is not None else None
            observation = None if candidate is None else dict(
                run_id=candidate.context[0], video_id=candidate.context[1],
                sequence=candidate.sample.sequence, received_at=candidate.sample.received_at,
                detections=candidate.result["detections"])
            demand = validator.validate(state, started, finished,
                                        explicit_selection=request is not None,
                                        mode=mode if request is not None else None,
                                        observation=observation)
            checked = _number(self.clock(), "local completion time")
            if checked < finished or checked - started > HTTP_TIMEOUT:
                raise PreviewError("Local yaw source exceeded its time budget")
            if demand.valid and checked >= demand.deadline:
                raise PreviewError("Console image expired while validating the source")
            if request is not None and not demand.valid:
                raise PreviewError("Radio selection did not produce a fresh visible target")
            context = (state["run_id"], state["video"]["source_id"])
            self._validator = validator
            if token is not None:
                self._selection_result = SelectionResult(token, checked, demand.selection_key, True)
            sample = SourceSample(demand, checked, False, self._selection_result)
        except Exception as exc:
            checked = self.clock()
            error = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")[:240]
            if token is not None:
                self._selection_result = SelectionResult(token, checked, None, False)
            sample = SourceSample(None, checked, True, self._selection_result)
            context = None
        wall_ms = max(0., self.clock() - started) * 1000
        cpu_ms = max(0., self.cpu_clock() - cpu_started) * 1000
        with self._lock:
            if self._closed:
                return
            self._sample = sample
            # A failed read cannot invent a newer observed source identity.
            if context is not None:
                self._context = context
            m = self._metrics
            m["count"] += 1
            m["errors"] += int(sample.error)
            m["selections"] += int(token is not None)
            if error is not None:
                m["last_error"] = error
            m["wall_ms_total"] += wall_ms
            m["cpu_ms_total"] += cpu_ms
            m["wall_ms_max"] = max(m["wall_ms_max"], wall_ms)
            m["cpu_ms_max"] = max(m["cpu_ms_max"], cpu_ms)
            m["last_wall_ms"], m["last_cpu_ms"] = wall_ms, cpu_ms

    def request_selection(self, token, mode):
        """Radio-thread entry: coalesce requests, capture identity, never touch session."""
        if (mode not in ("Y", "D") or not isinstance(token, str)
                or not re.fullmatch(r"[0-9a-f]{8}:(?:0|[1-9][0-9]{0,9})", token)
                or int(token.split(":")[1]) >= 2**31):
            raise PreviewError("Invalid radio selection identity")
        with self._lock:
            if self._closed:
                return
            self._request = token, self._context, self.clock(), mode
            if self._scheduled:
                return
            self._scheduled = True
        try:
            self.schedule(self._select_pending)
        except RuntimeError:
            # Closed owner loop: withdraw immediately, with no queued late action.
            self.close()

    def _select_pending(self):
        self._check_owner()
        with self._lock:
            request, self._request = self._request, None
            self._scheduled = False
            if self._closed:
                return
        if request is not None:
            self.publish(request)

    def close(self):
        with self._lock:
            self._closed = True
            self._request = None
            self._sample = SourceSample(None, self.clock(), True)
