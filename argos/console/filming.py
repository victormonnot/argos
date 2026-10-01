"""Per-take camera and assistance evidence, independent of flight authority.

The existing native recording remains authoritative for its original format.
This sidecar bundles full-cadence camera JPEGs and timestamped observations; it
does not claim aircraft telemetry or infer arming from the Pocket bridge.
"""
from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading

from .camera_recording import CameraRecorder


MAX_EVENT_BYTES = 128 * 1024
MAX_LOG_BYTES = 128 * 1024 * 1024
MAX_QUEUE_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 200_000
PILOT_MAX_AGE = .35


def label_value(value):
    if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError("Take label must be text of at most 80 characters without control characters")
    return value.strip()


def assistance_view(status, now):
    """Interpret a fresh Lua observation, never last-sent values as actuator feedback."""
    sample = status.get("pilot_sample")
    received = sample.get("received_at") if isinstance(sample, dict) else None
    age = (now - received if type(received) in (int, float)
           and math.isfinite(received) and 0 <= received <= now else None)
    fresh = bool(age is not None and age <= PILOT_MAX_AGE and status.get("connected"))
    correlated = fresh and all(
        status.get(key) is None or sample.get(key) == status.get(key)
        for key in ("session", "generation"))
    if correlated:
        correlated = (sample.get("state") == status.get("radio_state")
                      and sample.get("mode") == status.get("radio_mode", sample.get("mode")))
    mode = sample.get("mode") if correlated else None
    view = {"source": "pocket_lua_report", "fresh": bool(correlated),
            "selected_mode": "M" if mode == "N" else mode,
            "meaning": "Last reported Lua output; not native mixer or flight-controller feedback"}
    for axis in ("yaw", "pitch"):
        result = {"state": "unknown", "valid": False, "value": None}
        if correlated:
            phase = sample.get(axis + "_phase")
            actual_phase = status.get("radio_" + axis + "_phase")
            output = sample.get("lua_outputs", {}).get(axis, {})
            if actual_phase is not None and actual_phase != phase:
                pass  # A more recent status changed ownership after this sample.
            elif mode == "N" or (axis == "pitch" and mode != "D") or phase == "M":
                result = {"state": "manual", "valid": False, "value": None}
            elif phase == "R":
                result = {"state": "waiting", "valid": False, "value": None}
            elif sample.get("state") == "A" and phase == "A" and output.get("valid") is True:
                result = {"state": "assisted", "valid": True, "value": output.get("value")}
            else:
                result = {"state": "paused", "valid": False, "value": None}
        view[axis] = result
    return {"pilot_sample_age_s": age, "pilot_sample_fresh": fresh, "assistance": view}


class FilmingCapture:
    """Bounded producers, one event writer, one independently bounded JPEG writer."""

    def __init__(self, directory, *, identifier, run_id, started_at, clock,
                 label="", manifest=None, max_queue_bytes=MAX_QUEUE_BYTES,
                 max_log_bytes=MAX_LOG_BYTES, max_events=MAX_EVENTS,
                 camera_factory=CameraRecorder):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Invalid recording identifier")
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Invalid run identifier")
        if type(started_at) not in (int, float) or not math.isfinite(started_at) or started_at < 0:
            raise ValueError("Invalid recording start time")
        for value in (max_queue_bytes, max_log_bytes, max_events):
            if type(value) is not int or value < 1:
                raise ValueError("Recording bounds must be positive integers")
        self.directory = Path(directory)
        self.label = label_value(label)
        self.identifier, self.run_id = identifier, run_id
        self.started_at, self.clock = started_at, clock
        self.max_queue_bytes, self.max_log_bytes, self.max_events = max_queue_bytes, max_log_bytes, max_events
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._queue = deque()
        self._queued_bytes = self._inflight_bytes = 0
        self._written = self._bytes = self._dropped = self._failed = 0
        self._contention_drops = {"radio": 0, "vision": 0}
        self._pilot_samples = 0
        self._last_pilot = None
        self._last_frame = None
        self._error = ""
        self._stopping = False
        self._finished = False
        self._ended_at = None
        self._last_valid_at = started_at
        self._reason = None
        self._video = self._video_sink = None
        self._service = None
        self._radio_sink = self.record_radio
        self._manifest = {"format": "argos.filming", "schema_version": 1,
            "id": identifier, "run_id": run_id, "label": self.label,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "started_at": started_at, "clock": "console run monotonic seconds",
            "radio_time": "Pocket observations stamped on host receipt; no FC or camera-exposure clock",
            "provenance": deepcopy(manifest or {}),
            "files": {"camera": "camera.mjpeg", "camera_index": "camera.frames.jsonl",
                      "camera_manifest": "camera.json", "events": "events.jsonl"},
            "related_native_files": [f"../{identifier}.jsonl", f"../{identifier}.visual.sqlite3"],
            "limits": {"max_queue_bytes": max_queue_bytes, "max_log_bytes": max_log_bytes,
                       "max_events": max_events}}
        self.directory.mkdir(parents=True, exist_ok=False)
        try:
            self.camera = camera_factory(self.directory, session_id=identifier,
                                         started_at=started_at, clock=clock)
            self._write_manifest()
            self._thread = threading.Thread(target=self._run, name="argos-filming-events", daemon=True)
            self._thread.start()
        except Exception:
            if hasattr(self, "camera"):
                self.camera.stop(started_at, reason="startup_error", timeout=0)
            raise

    def attach_video(self, video, source_id):
        """Called by the console owner on initial attach and camera replacement."""
        if self._video is not None:
            self._video.remove_frame_sink(self._video_sink)
        def submit(frame):
            return self.camera.submit(replace(frame, source_id=source_id))
        self._video, self._video_sink = video, submit
        video.add_frame_sink(submit)
        with self._lock:
            stopped = self._stopping
        if stopped:
            video.remove_frame_sink(submit)

    def attach_radio(self, service):
        self._service = service
        service.add_status_sink(self._radio_sink)
        with self._lock:
            stopped = self._stopping
        if stopped:
            service.remove_status_sink(self._radio_sink)

    def _enqueue(self, kind, payload):
        # Serialize outside the queue lock. Each kind has exactly one producer
        # (radio worker / console owner); its contention counter never waits.
        if self._stopping or self._error:
            return False
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < self.started_at:
            if self._lock.acquire(blocking=False):
                try:
                    self._error = "Invalid filming event clock"
                    self._failed += 1
                    self._wake.set()
                finally:
                    self._lock.release()
            else:
                self._contention_drops[kind] += 1
            return False
        record = {"schema_version": 1, "kind": kind, "run_id": self.run_id,
                  "recording_id": self.identifier, "at": now,
                  "elapsed_s": now - self.started_at, **payload}
        encoding_error = ""
        try:
            line = (json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n").encode()
        except (TypeError, ValueError, OverflowError):
            encoding_error = "Invalid filming event data"
            line = b""
        if not self._lock.acquire(blocking=False):
            self._contention_drops[kind] += 1
            return False
        try:
            if self._stopping or self._error:
                return False
            if encoding_error:
                self._error = encoding_error
                self._failed += 1
                self._wake.set()
                return False
            if len(line) > MAX_EVENT_BYTES:
                self._error = "Filming event exceeds size bound"
                self._failed += 1
                self._wake.set()
                return False
            if self._queued_bytes + self._inflight_bytes + len(line) > self.max_queue_bytes:
                self._dropped += 1
                return False
            self._queue.append(line)
            self._queued_bytes += len(line)
            self._last_valid_at = max(self._last_valid_at, now)
            if kind == "radio":
                sample = payload["status"].get("pilot_sample")
                if isinstance(sample, dict):
                    key = (sample.get("session"), sample.get("generation"), sample.get("received_at"))
                    if key != self._last_pilot:
                        self._last_pilot = key
                        self._pilot_samples += 1
            self._wake.set()
            return True
        finally:
            self._lock.release()

    def record_radio(self, status):
        anchor = None
        if self._service is not None:
            before = self.clock()
            host = self._service.clock()
            after = self.clock()
            anchor = {"console_before": before, "host_monotonic_at": host,
                      "console_after": after}
        return self._enqueue("radio", {"status": status, "clock_anchor": anchor})

    def record_vision(self, candidate, *, source_id, preview):
        if candidate is None:
            return False
        key = (source_id, candidate.sample.sequence)
        if key == self._last_frame or candidate.sample.received_at < self.started_at:
            return False
        self._last_frame = key
        return self._enqueue("vision", {"video_id": source_id,
            "frame_sequence": candidate.sample.sequence,
            "image_received_at": candidate.sample.received_at,
            "image_elapsed_s": candidate.sample.received_at - self.started_at,
            "result": {k: v for k, v in candidate.result.items()
                       if k in {"width", "height", "detections", "inference_ms", "model", "variant"}},
            "yaw_preview": preview})

    def status(self):
        camera = self.camera.status()
        with self._lock:
            state = "complete" if self._finished else "finalizing" if self._stopping else "recording"
            error = self._error or (camera.get("error") or camera.get("detail")
                                    if camera.get("state") == "error" else "")
            if error:
                state = "error"
            return {"state": state, "id": self.identifier, "label": self.label,
                    "directory": str(self.directory), "started_at": self.started_at,
                    "ended_at": self._ended_at, "end_reason": self._reason,
                    "error": error, "events": self._written, "bytes": self._bytes,
                    "dropped_events": self._dropped + sum(self._contention_drops.values()),
                    "contention_drops": dict(self._contention_drops), "failed_events": self._failed,
                    "pending_events": len(self._queue) + bool(self._inflight_bytes),
                    "pilot_samples": self._pilot_samples, "camera": camera,
                    "pilot_required": self._service is not None,
                    "pilot_data_available": self._pilot_samples > 0,
                    "writer_stopped": self._finished,
                    "complete": self._finished and not error and not self._dropped
                                and not any(self._contention_drops.values())
                                and not self._failed and camera.get("complete", False)
                                and (self._service is None or self._pilot_samples > 0)}

    def _write_manifest(self):
        value = {**self._manifest, **self.status()}
        temporary = self.directory / "manifest.json.tmp"
        temporary.write_text(json.dumps(value, allow_nan=False, indent=2) + "\n")
        temporary.replace(self.directory / "manifest.json")

    def _run(self):
        try:
            with (self.directory / "events.jsonl").open("xb") as output:
                while True:
                    self._wake.wait()
                    self._wake.clear()
                    while True:
                        with self._lock:
                            if not self._queue:
                                stopping = self._stopping
                                break
                            line = self._queue.popleft()
                            self._queued_bytes -= len(line)
                            self._inflight_bytes = len(line)
                        if self._bytes + len(line) > self.max_log_bytes or self._written >= self.max_events:
                            with self._lock:
                                self._error = "Filming event file limit reached"
                                self._failed += 1
                                self._inflight_bytes = 0
                            continue
                        try:
                            output.write(line)
                        except OSError:
                            with self._lock:
                                self._failed += 1
                            raise
                        finally:
                            with self._lock:
                                self._inflight_bytes = 0
                        with self._lock:
                            self._bytes += len(line)
                            self._written += 1
                    output.flush()
                    if self._error:
                        self.stop(reason="event_writer_error", timeout=0)
                        stopping = True
                    if stopping:
                        break
        except (OSError, ValueError) as exc:
            with self._lock:
                self._error = f"Filming event writer failed: {exc}"
                self._failed += len(self._queue)
                self._queue.clear()
                self._queued_bytes = 0
        finally:
            # Closing the raw stream is independent of flight workers. It has
            # already been told to stop by the nonblocking owner operation.
            self.stop(reason=self._reason or "event_writer_error", timeout=0)
            while True:
                camera = self.camera.stop(self._ended_at, reason=self._reason, timeout=1.)
                if camera["writer_stopped"]:
                    break
            with self._lock:
                self._ended_at = max(self._ended_at, camera.get("ended_at") or self._ended_at)
                self._finished = True
            try:
                self._write_manifest()
            except (OSError, ValueError) as exc:
                with self._lock:
                    self._error = f"Cannot finalize filming manifest: {exc}"

    def stop(self, now=None, *, reason="stopped", timeout=0):
        if now is None:
            now = self.clock()
        with self._lock:
            first = not self._stopping
            if first:
                if (type(now) not in (int, float) or not math.isfinite(now)
                        or now < self.started_at):
                    self._error = self._error or "Invalid filming stop clock"
                    now = self._last_valid_at
                self._stopping = True
                self._ended_at = max(self._last_valid_at, now)
                self._reason = reason
        if first:
            if self._video is not None:
                self._video.remove_frame_sink(self._video_sink)
            if self._service is not None:
                self._service.remove_status_sink(self._radio_sink)
            self.camera.stop(self._ended_at, reason=reason, timeout=0)
            self._wake.set()
        if timeout:
            self._thread.join(timeout=timeout)
        return self.status()
