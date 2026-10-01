"""Bounded full-cadence recording of camera JPEGs before console overlays.

The MJPEG file is the exact concatenation of JPEGs already encoded by capture.
Its JSONL index is authoritative for timing: host receipt is not sensor exposure
or physical motion time. A dedicated writer performs all frame IO; admission
never waits for disk or queue capacity and never opens or encodes a camera.
"""

from __future__ import annotations

from collections import deque
import json
import math
from pathlib import Path
from threading import Event, Lock, Thread
import time
from typing import Callable

from .video import CameraFrame, MAX_DIMENSION, MAX_JPEG_BYTES, MAX_PIXELS


MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_FRAMES = 216_000
MAX_DURATION_S = 3600.
MAX_QUEUE_BYTES = 64 * 1024 * 1024
MAX_QUEUE_FRAMES = 128
MAX_INDEX_LINE_BYTES = 8192
MEDIA_NAME = "camera.mjpeg"
INDEX_NAME = "camera.frames.jsonl"
MANIFEST_NAME = "camera.json"


def _number(value, name, *, positive=False):
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and value >= 0 and (not positive or value > 0))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return float(value)


def _integer(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


class CameraRecorder:
    """One battery's original camera media with visible loss accounting.

    The byte limit bounds MJPEG payload; the frame count and per-line limit
    separately bound the index. Queue bounds include the frame being written.
    Limit exhaustion ends capture; queue pressure drops arriving frames and
    marks the archive incomplete. A stop timeout leaves state ``finalizing`` and
    the writer owns its files until ``writer_stopped`` becomes true.
    """

    def __init__(self, directory: Path, *, session_id: str, started_at: float,
                 clock: Callable[[], float] = time.monotonic,
                 max_duration_s=1800., max_bytes=MAX_BYTES,
                 max_frames=MAX_FRAMES, max_queue_bytes=MAX_QUEUE_BYTES,
                 max_queue_frames=MAX_QUEUE_FRAMES):
        if (not isinstance(session_id, str) or not session_id
                or len(session_id) > 128 or any(ord(c) < 32 for c in session_id)):
            raise ValueError("session_id must be a nonempty bounded identifier")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.directory = Path(directory)
        self.session_id = session_id
        self.started_at = _number(started_at, "started_at")
        self.clock = clock
        self.max_duration_s = _number(max_duration_s, "max_duration_s", positive=True)
        if self.max_duration_s > MAX_DURATION_S:
            raise ValueError(f"max_duration_s exceeds {MAX_DURATION_S}")
        self.max_bytes = _integer(max_bytes, "max_bytes", MAX_BYTES)
        self.max_frames = _integer(max_frames, "max_frames", MAX_FRAMES)
        self.max_queue_bytes = _integer(max_queue_bytes, "max_queue_bytes", MAX_QUEUE_BYTES)
        self.max_queue_frames = _integer(max_queue_frames, "max_queue_frames", MAX_QUEUE_FRAMES)
        self._lock = Lock()
        self._wake = Event()
        self._finished = Event()
        self._queue = deque()
        self._accepting = True
        self._ended_at = None
        self._reason = ""
        self._error = ""
        self._state = "recording"
        self._stop_timed_out = False
        self._accepted = self._written = self._written_bytes = 0
        self._index_bytes = self._buffered_bytes = self._buffered_frames = 0
        self._reserved_bytes = 0
        self._dropped_queue = self._dropped_invalid = self._dropped_limit = 0
        self._dropped_contention = self._discarded_error = 0
        self._first_received_at = self._last_received_at = None
        self._last_written_at = None
        self._sources = []
        self._source_ids = set()
        self._sources_truncated = False
        self._streams = []
        created = []
        try:
            # Exclusive creation prevents overwriting a prior battery's media.
            # Startup runs on the lifecycle caller, never a camera callback.
            for name in (MEDIA_NAME, INDEX_NAME, MANIFEST_NAME):
                path = self.directory / name
                self._streams.append(path.open("xb", buffering=0))
                created.append(path)
            self._media, self._index, self._manifest = self._streams
            self._thread = Thread(target=self._run, name="argos-camera-recorder", daemon=True)
            self._thread.start()
        except Exception:
            for stream in self._streams:
                stream.close()
            for path in created:
                path.unlink(missing_ok=True)
            raise

    def _end_locked(self, now, reason):
        if self._accepting:
            self._accepting = False
            # A lifecycle caller can sample time just before the producer
            # admits its last image. Never place that image after archive end.
            self._ended_at = max(self.started_at, self._last_received_at or self.started_at, now)
            self._reason = reason
            self._state = "finalizing"
            self._wake.set()

    def submit(self, frame: CameraFrame) -> bool:
        """Enqueue a retained JPEG reference without encoding or waiting."""
        if not self._lock.acquire(blocking=False):
            # VideoStore serializes callbacks on its producer lock. Keep this
            # sole-producer counter outside the lock to avoid waiting on status.
            if self._accepting:
                self._dropped_contention += 1
            return False
        try:
            if not self._accepting:
                return False
            try:
                sample = frame.sample
                received_at = _number(sample.received_at, "received_at")
                if (not isinstance(frame, CameraFrame) or not isinstance(sample.jpeg, bytes)
                        or not 4 <= len(sample.jpeg) <= MAX_JPEG_BYTES
                        or not sample.jpeg.startswith(b"\xff\xd8")
                        or not sample.jpeg.endswith(b"\xff\xd9")
                        or type(sample.sequence) is not int or sample.sequence < 1
                        or type(frame.width) is not int or type(frame.height) is not int
                        or not 0 < frame.width <= MAX_DIMENSION
                        or not 0 < frame.height <= MAX_DIMENSION
                        or frame.width * frame.height > MAX_PIXELS
                        or frame.source not in {"device", "gazebo"}
                        or not isinstance(frame.endpoint, str) or len(frame.endpoint) > 1024
                        or not isinstance(frame.source_id, str) or len(frame.source_id) > 64
                        or received_at < self.started_at
                        or (self._last_received_at is not None and received_at < self._last_received_at)):
                    raise ValueError("invalid camera frame or backwards receive timestamp")
                if frame.source_stamp is not None:
                    sec, nsec = frame.source_stamp
                    if type(sec) is not int or type(nsec) is not int or not 0 <= nsec < 1_000_000_000:
                        raise ValueError("invalid source timestamp")
            except (AttributeError, TypeError, ValueError, OverflowError):
                self._dropped_invalid += 1
                return False
            if received_at - self.started_at >= self.max_duration_s:
                self._dropped_limit += 1
                self._end_locked(self.started_at + self.max_duration_s, "duration_limit")
                return False
            size = len(sample.jpeg)
            if self._reserved_bytes + size > self.max_bytes or self._accepted >= self.max_frames:
                self._dropped_limit += 1
                self._end_locked(received_at, "size_limit" if self._reserved_bytes + size > self.max_bytes else "frame_limit")
                return False
            if (self._buffered_frames >= self.max_queue_frames
                    or self._buffered_bytes + size > self.max_queue_bytes):
                self._dropped_queue += 1
                return False
            self._queue.append(frame)
            self._accepted += 1
            self._reserved_bytes += size
            self._buffered_bytes += size
            self._buffered_frames += 1
            if self._first_received_at is None:
                self._first_received_at = received_at
            self._last_received_at = received_at
            self._wake.set()
            return True
        finally:
            self._lock.release()

    def _status_locked(self, now):
        dropped = self._dropped_queue + self._dropped_invalid + self._dropped_limit + self._dropped_contention
        span = (None if self._first_received_at is None or self._last_written_at is None
                else self._last_written_at - self._first_received_at)
        return {
            "schema": 1, "session_id": self.session_id, "state": self._state,
            "started_at": self.started_at, "ended_at": self._ended_at,
            "elapsed_s": max(0., (self._ended_at if self._ended_at is not None else now) - self.started_at),
            "reason": self._reason, "error": self._error,
            "media": MEDIA_NAME, "index": INDEX_NAME,
            "format": "mjpeg_with_receive_timestamp_index",
            "timestamp_basis": "host_monotonic_camera_receipt_not_exposure",
            "image_basis": "existing_capture_jpeg_without_argos_overlays_camera_osd_preserved",
            "accepted_frames": self._accepted, "written_frames": self._written,
            "size_bytes": self._written_bytes, "index_bytes": self._index_bytes,
            "buffered_frames": self._buffered_frames, "buffered_bytes": self._buffered_bytes,
            "dropped_frames": dropped, "dropped_queue": self._dropped_queue,
            "dropped_contention": self._dropped_contention, "dropped_invalid": self._dropped_invalid,
            "dropped_limit": self._dropped_limit, "discarded_error": self._discarded_error,
            "first_received_at": self._first_received_at, "last_received_at": self._last_written_at,
            "observed_fps": (self._written - 1) / span if span and span > 0 and self._written > 1 else None,
            "complete": (self._state == "complete" and self._written > 0 and dropped == 0
                         and not self._error and self._reason not in {"duration_limit", "size_limit", "frame_limit"}),
            "empty_capture": self._written == 0,
            "writer_stopped": self._finished.is_set(), "stop_timed_out": self._stop_timed_out,
            "max_bytes": self.max_bytes, "max_frames": self.max_frames,
            "max_duration_s": self.max_duration_s, "max_queue_frames": self.max_queue_frames,
            "max_queue_bytes": self.max_queue_bytes, "max_index_line_bytes": MAX_INDEX_LINE_BYTES,
            "sources": [item.copy() for item in self._sources], "sources_truncated": self._sources_truncated,
        }

    def status(self) -> dict:
        try:
            now = _number(self.clock(), "clock")
        except (TypeError, ValueError, OverflowError):
            now = self.started_at
        with self._lock:
            return self._status_locked(now)

    def stop(self, now=None, *, reason="user", timeout=5.) -> dict:
        """Stop admission and drain already accepted frames within a wait bound."""
        now = _number(self.clock() if now is None else now, "now")
        timeout = _number(timeout, "timeout")
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            raise ValueError("reason must be a bounded string")
        with self._lock:
            self._end_locked(now, reason)
        self._thread.join(timeout=timeout)
        with self._lock:
            self._stop_timed_out = timeout > 0 and self._thread.is_alive()
            return self._status_locked(now)

    def _write_frame(self, frame):
        sample = frame.sample
        row = {
            "schema": 1, "session_id": self.session_id, "frame": self._written,
            "sequence": sample.sequence, "received_at": sample.received_at,
            "elapsed_s": sample.received_at - self.started_at,
            "source": frame.source, "endpoint": frame.endpoint, "source_id": frame.source_id,
            "width": frame.width, "height": frame.height,
            "source_stamp": (None if frame.source_stamp is None else
                             {"sec": frame.source_stamp[0], "nsec": frame.source_stamp[1]}),
            "offset": self._written_bytes, "size_bytes": len(sample.jpeg),
        }
        line = (json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        if len(line) > MAX_INDEX_LINE_BYTES:
            raise ValueError("camera index line exceeds byte bound")
        if self._media.write(sample.jpeg) != len(sample.jpeg) or self._index.write(line) != len(line):
            raise OSError("short camera archive write")
        with self._lock:
            self._written += 1
            self._written_bytes += len(sample.jpeg)
            self._index_bytes += len(line)
            self._last_written_at = sample.received_at
            identity = (frame.source_id, frame.source, frame.endpoint)
            if identity not in self._source_ids:
                if len(self._sources) < 32:
                    self._source_ids.add(identity)
                    self._sources.append({"source_id": frame.source_id, "source": frame.source,
                                          "endpoint": frame.endpoint, "first_received_at": sample.received_at})
                else:
                    self._sources_truncated = True

    def _write_manifest(self):
        with self._lock:
            data = self._status_locked(self._ended_at or self.started_at)
            # Persisted terminal metadata describes the closed archive. Runtime
            # callers still use the event until all file handles are closed.
            data["writer_stopped"] = self._state in {"complete", "error"}
        raw = (json.dumps(data, indent=2, allow_nan=False) + "\n").encode("utf-8")
        self._manifest.seek(0)
        if self._manifest.write(raw) != len(raw):
            raise OSError("short camera manifest write")
        self._manifest.truncate()
        self._manifest.flush()

    def _run(self):
        try:
            self._write_manifest()
            while True:
                self._wake.wait(.1)
                now = _number(self.clock(), "clock")
                with self._lock:
                    if now - self.started_at >= self.max_duration_s:
                        self._end_locked(self.started_at + self.max_duration_s, "duration_limit")
                    frame = self._queue.popleft() if self._queue else None
                    if frame is None:
                        if not self._accepting:
                            break
                        self._wake.clear()
                        continue
                self._write_frame(frame)
                with self._lock:
                    self._buffered_bytes -= len(frame.sample.jpeg)
                    self._buffered_frames -= 1
            self._media.flush()
            self._index.flush()
            with self._lock:
                self._state = "complete"
                self._stop_timed_out = False
        except Exception as exc:
            with self._lock:
                self._end_locked(self._last_received_at or self.started_at, "error")
                self._state = "error"
                self._reason = "error"
                self._error = str(exc)[:400]
                self._discarded_error = self._accepted - self._written
                self._queue.clear()
                self._buffered_bytes = self._buffered_frames = 0
            # A failed frame must not leave an unindexed tail in a healthy file.
            for stream, length in ((self._media, self._written_bytes), (self._index, self._index_bytes)):
                try:
                    stream.seek(length)
                    stream.truncate()
                    stream.flush()
                except Exception:
                    pass
        finally:
            try:
                self._write_manifest()
            except Exception as exc:
                with self._lock:
                    self._state = "error"
                    self._error = f"Camera manifest could not be finalized: {exc}"[:400]
            for stream in self._streams:
                try:
                    stream.close()
                except Exception as exc:
                    with self._lock:
                        self._state = "error"
                        self._error = f"Camera archive could not be closed: {exc}"[:400]
            self._finished.set()
