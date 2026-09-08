"""Optional image-only perception, isolated from the MAVLink event-loop owner.

One image can be in flight and one completed image retained. Boxes always travel
with their original JPEG; their age is camera receipt age, not exposure time.
No simulator poses, telemetry or flight command transport enter the worker.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import multiprocessing
import math
from pathlib import Path
from queue import Empty, Full
import time

from argos.perception.image_tracks import ImageTracker
from .video import VideoSample

MAX_HZ = 5
AGE_LIMIT = 1.
START_TIMEOUT = 20.
INFERENCE_TIMEOUT = 5.


def _worker(model_path, incoming, outgoing):
    try:
        from argos.perception.yolox import YoloXPersonDetector
        detector = YoloXPersonDetector(model_path)
        outgoing.put(("ready", None))
        while True:
            job = incoming.get()
            if job is None:
                return
            identifier, jpeg = job
            outgoing.put(("result", (identifier, detector.detect(jpeg))))
    except Exception as exc:
        outgoing.put(("error", f"Vision unavailable: {type(exc).__name__}: {exc}"[:400]))


@dataclass(frozen=True)
class AnalyzedFrame:
    sample: VideoSample
    context: tuple[str, str]
    result: dict


class VisionService:
    """App-owned process survives source replacement, but no track crosses it.

    tick/state/frame are short, nonblocking calls on the event-loop owner. A
    stalled worker is terminated, never allowed to accumulate a second worker.
    Model recovery requires restarting the console; manual flight stays separate.
    """

    def __init__(self, model_path: Path | None, *, wall_clock=time.monotonic,
                 process_context=None):
        self.model_path = model_path
        self.wall_clock = wall_clock
        self._mp = process_context
        self._process = self._incoming = self._outgoing = None
        self._started_at = self._submitted_at = self._last_submit = None
        self._ready = self._closed = False
        self._error = ""
        self._context = None
        self._pending = self._frame = None
        self._last_sequence = None
        self._identifier = self._processed = 0
        self._tracker = ImageTracker()
        self._selection_history = deque(maxlen=4)

    def start(self):
        if self.model_path is None or self._process is not None or self._closed:
            return
        try:
            ctx = self._mp or multiprocessing.get_context("spawn")
            self._incoming, self._outgoing = ctx.Queue(maxsize=1), ctx.Queue(maxsize=1)
            self._process = ctx.Process(target=_worker, args=(str(self.model_path), self._incoming, self._outgoing), daemon=True)
            self._started_at = self.wall_clock()
            self._process.start()
        except Exception as exc:
            self._fail(f"Unable to start vision: {exc}")

    def _fail(self, detail):
        self._error = str(detail)[:400]
        self._pending = self._frame = None
        self._tracker.reset()
        try:
            if self._process is not None and self._process.is_alive():
                self._process.terminate()
        except (OSError, ValueError, AssertionError):
            pass

    @staticmethod
    def _identity(session):
        return session.run_id, session.video_source_id

    def _bind(self, session):
        context = self._identity(session)
        if context != self._context:
            self._context = context
            self._frame = None
            self._last_sequence = None
            self._tracker.reset()
            self._selection_history.clear()
            self._processed = 0
            # Leave the one outstanding job alone until it returns or times out.
            # Its original context below prevents acceptance by the new source.

    def tick(self, session):
        try:
            self._tick(session)
        except Exception as exc:
            # Optional perception failure must never stop manual/GCS servicing.
            self._fail(f"Vision unavailable: {type(exc).__name__}: {exc}")

    def _tick(self, session):
        self._bind(session)
        if self.model_path is None or self._closed or self._error or self._process is None:
            return
        wall_now = self.wall_clock()
        now = session.clock()
        try:
            # At most one queued response: drain bounded work, never await DNN.
            kind, value = self._outgoing.get_nowait()
        except Empty:
            kind, value = None, None
        if kind == "error":
            self._fail(value)
            return
        if kind == "ready":
            self._ready = True
        elif kind == "result" and self._pending is not None:
            identifier, result = value
            pending_id, candidate = self._pending
            if identifier == pending_id:
                self._pending = None
                if (candidate.context == self._context
                        and 0 <= now - candidate.sample.received_at <= self._age_limit(session)
                        and session.video.latest(now) is not None):
                    try:
                        result = self._validate_result(result)
                        result["detections"] = self._tracker.update(result["detections"], candidate.sample.received_at)
                    except (TypeError, ValueError, KeyError) as exc:
                        self._fail(f"Invalid vision result: {exc}")
                        return
                    self._frame = AnalyzedFrame(candidate.sample, candidate.context, result)
                    self._processed += 1
                    self._selection_history.append((candidate.context, candidate.sample.sequence,
                        candidate.sample.received_at, frozenset(d["track_id"] for d in result["detections"])))
        if not self._process.is_alive():
            self._fail("Vision worker stopped; restart the console to retry")
            return
        if not self._ready:
            if wall_now - self._started_at > START_TIMEOUT:
                self._fail("Vision model startup timed out")
            return
        if self._pending is not None:
            if wall_now - self._submitted_at > INFERENCE_TIMEOUT:
                self._fail("Vision inference timed out; manual flight remains available")
            return
        if self._last_submit is not None and wall_now - self._last_submit < 1 / MAX_HZ:
            return
        sample = session.video.latest(now)
        if (sample is None or sample.sequence == self._last_sequence
                or (self._frame is not None and self._frame.context == self._context
                    and sample.received_at <= self._frame.sample.received_at)):
            return
        self._identifier += 1
        try:
            self._incoming.put_nowait((self._identifier, sample.jpeg))
        except Full:
            return
        self._pending = (self._identifier, AnalyzedFrame(sample, self._context, {}))
        self._last_sequence = sample.sequence
        self._submitted_at = self._last_submit = wall_now

    @staticmethod
    def _validate_result(result):
        if not isinstance(result, dict) or set(result) != {"width", "height", "inference_ms", "detections"}:
            raise ValueError("unexpected detector metadata")
        result = dict(result)
        for key in ("width", "height"):
            if type(result[key]) is not int or not 1 <= result[key] <= 4096:
                raise ValueError("invalid image dimensions")
        elapsed = result["inference_ms"]
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("invalid inference duration")
        detections = result["detections"]
        if not isinstance(detections, list) or len(detections) > 16:
            raise ValueError("too many detections")
        for item in detections:
            if not isinstance(item, dict) or set(item) != {"box", "confidence"}:
                raise ValueError("invalid detection")
            box, confidence = item["box"], item["confidence"]
            if (not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                    or box[2] <= 0 or box[3] <= 0
                    or box[0] + box[2] > 1.000001 or box[1] + box[3] > 1.000001
                    or type(confidence) not in (int, float) or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1):
                raise ValueError("invalid detection bounds or confidence")
        return result

    @staticmethod
    def _age_limit(session):
        return min(AGE_LIMIT, session.config.video_age)

    def frame(self, session):
        now = session.clock()
        candidate = self._frame
        if (self._closed or self._error or candidate is None
                or candidate.context != self._identity(session)
                or not 0 <= now - candidate.sample.received_at <= self._age_limit(session)
                or session.video.latest(now) is None):
            return None
        return candidate

    def check_selection(self, session, values, now):
        """Validate a displayed click using bounded metadata, never browser boxes."""
        context = values["run_id"], values["video_id"]
        if context != self._identity(session):
            raise RuntimeError("The selected camera source changed")
        if not any(ctx == context and sequence == values["frame_sequence"]
                   and 0 <= now - at <= .75 and values["track_id"] in ids
                   for ctx, sequence, at, ids in self._selection_history):
            raise RuntimeError("The displayed detection expired; select a current person")
        current = self.frame(session)
        if (current is None or now - current.sample.received_at > .45
                or not any(d["track_id"] == values["track_id"] for d in current.result["detections"])):
            raise RuntimeError("The selected person is no longer visible in a recent image")

    def state(self, session):
        candidate = self.frame(session)
        same_source = self._frame is not None and self._frame.context == self._identity(session)
        age = max(0., session.clock() - self._frame.sample.received_at) if same_source else None
        if self.model_path is None:
            state, detail = "disabled", "Person detection is not configured"
        elif self._closed or self._error:
            state, detail = "error", self._error or "Vision stopped"
        elif not self._ready:
            state, detail = "starting", "Loading the person detector"
        elif candidate is not None:
            state, detail = "recent", "Image-based person detection and tracking; manual flight"
        elif same_source:
            state, detail = "stale", "No recent analyzed image"
        else:
            state, detail = "waiting", "Waiting for a recent camera image"
        return {"configured": self.model_path is not None, "state": state, "detail": detail,
                "model": "YOLOX-Tiny", "max_hz": MAX_HZ, "age_limit_s": self._age_limit(session),
                "frame_age_s": age, "inference_ms": candidate.result["inference_ms"] if candidate else None,
                "processed": self._processed if self._context == self._identity(session) else 0,
                "tracks": len(candidate.result["detections"]) if candidate else 0}

    async def aclose(self):
        self._closed = True
        self._frame = self._pending = None
        process = self._process
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 1.)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 1.)
            if not process.is_alive():
                process.close()
        for queue in (self._incoming, self._outgoing):
            if queue is not None:
                queue.cancel_join_thread()
                queue.close()
