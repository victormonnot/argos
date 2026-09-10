"""Bounded, optional visual sidecars for telemetry journals.

The receiver only validates/copies bounded public metadata and queues immutable
JPEG bytes. One writer thread owns SQLite and all disk work. A full queue ends
the visual capture visibly; it never back-pressures flight control. Completion
is committed before an archive can expose a sidecar. Times are local session
receipts/availability, never camera exposure or simulator ground truth.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import sqlite3
import stat
from threading import Event, Lock, Thread
import time
from uuid import uuid4

from .archive import ArchiveError
from .framing_analysis import MAX_REPORT_EVENTS, framing_report


MAX_BYTES = 256 * 1024 * 1024
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_ROW_BYTES = 64 * 1024
MAX_SAMPLES = 36_000
MAX_EVENTS = 20_000
MAX_DURATION = 3600.
MAX_QUEUE = 32
MAX_HZ = 10
SAMPLE_AGE = .35
RESERVE_BYTES = 64 * 1024
IDENTIFIER = re.compile(r"[0-9a-f]{32}")
SCHEMA = {
    "recording": "CREATE TABLE recording (id INTEGER PRIMARY KEY CHECK(id=1), metadata TEXT NOT NULL)",
    "frames": "CREATE TABLE frames (idx INTEGER PRIMARY KEY, available REAL NOT NULL, received REAL NOT NULL, sequence INTEGER NOT NULL, video_id TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, vision TEXT NOT NULL, digest TEXT NOT NULL, jpeg BLOB NOT NULL)",
    "samples": "CREATE TABLE samples (idx INTEGER PRIMARY KEY, at REAL NOT NULL, frame_idx INTEGER, control TEXT NOT NULL)",
    "events": "CREATE TABLE events (idx INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, status TEXT)",
    "samples_at": "CREATE INDEX samples_at ON samples (at, idx)",
    "events_at": "CREATE INDEX events_at ON events (at, idx)",
}


def _number(value, *, minimum=0., maximum=1e15):
    if type(value) not in (float, int) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Invalid visual recording number")
    return float(value)


def _identifier(value):
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Invalid visual recording identifier")
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid visual recording integer")
    return value


def _text(value, maximum=400):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Invalid visual recording text")
    return "".join(c if c >= " " or c in "\n\t" else " " for c in value)


def _json(value):
    result = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if len(result) > MAX_ROW_BYTES:
        raise ValueError("Visual metadata exceeds the row limit")
    return result


def _check_jpeg(jpeg, width, height):
    """Decode one bounded image on a disk worker, never the receiver loop."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(jpeg)) as picture:
            if picture.format != "JPEG" or picture.size != (width, height):
                raise ValueError("Archived image dimensions disagree with metadata")
            picture.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Archived image exceeds decoded pixel limits") from exc


# Explicit public fields only. In particular no token, request/body, transport
# endpoint, arbitrary profile, or caller-provided nested object is recorded.
CONTROL_FIELDS = {
    "enabled", "available", "owned", "phase", "axes", "selected_mode", "throttle",
    "mode_generation", "mode_transition", "mode_transfer", "prepared", "input_seq",
    "framing", "vehicle", "command", "interruption", "last_error", "reason", "at",
}
PUBLIC_FIELDS = CONTROL_FIELDS | {
    "forward", "right", "up", "yaw", "roll", "pitch", "armed", "mode", "landed",
    "heartbeat_age", "landed_state", "at", "started_at", "ended_at", "deadline",
    "from", "to", "source_mode", "target_mode", "generation", "completed_at",
    "throttle_percent", "stabilize_throttle", "bridge_throttle", "transferred_at",
    "name", "id", "status", "result", "detail", "accepted", "observed", "sent_at",
    "ack_at", "observed_at", "requested_at", "target_id", "revision", "output",
    "reference", "reference_height", "reference_height_fraction", "current_height",
    "height", "height_ratio", "distance", "distance_reference", "confidence",
    "target", "box", "x", "y", "w", "h", "age", "age_s", "age_limit_s",
    "frame_age_s", "frame_sequence", "received_at", "sequence", "video_id", "run_id",
    "pause", "pause_deadline", "pause_remaining_s", "takeover_remaining_s", "last_loss",
    "law", "diagnostics", "diagnostic", "evidence", "error", "loss", "reason_code",
    "axes_neutral", "can_engage", "can_adjust", "adjustment", "height_error",
    "center_error", "horizontal_error", "vertical_error", "error_x", "error_y",
    "target_height", "reference_height_pct", "observed_height", "measured_height",
    "target_age_s", "track_id", "track_age", "track_state", "source", "input_age_s",
    "lease_started_at", "input_seq", "can_switch", "requested_mode", "input_at",
    "mavlink_z", "thrust", "hover", "kind", "state", "bound", "value", "limit",
    "image", "detections", "selected", "observed_mode", "requested", "ack_result", "active", "paused",
    "from_mode", "to_mode", "target_throttle", "action", "command_id", "transport", "ack",
    "framing_loss", "evidence_at",
}


def _public(value, *, top=False, depth=0):
    if depth > 7:
        return None
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        return value if math.isfinite(value) and abs(value) <= 1e15 else None
    if isinstance(value, str):
        return _text(value[:400])
    if isinstance(value, dict):
        allowed = CONTROL_FIELDS if top else PUBLIC_FIELDS
        return {key: _public(item, depth=depth + 1) for key, item in value.items()
                if key in allowed or (not top and isinstance(item, str)
                                      and ((key == "profile" and item in ("full", "pilot_throttle"))
                                           or (key == "range_response" and item in ("gentle", "normal", "responsive"))))}
    if isinstance(value, (list, tuple)):
        return [_public(item, depth=depth + 1) for item in value[:64]]
    return None


def _vision(value, frame):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid visual detection metadata")
    if value.get("frame_sequence") != frame["sequence"] or value.get("video_id") != frame["video_id"]:
        return None
    detections = value.get("detections")
    if not isinstance(detections, list) or len(detections) > 64:
        raise ValueError("Invalid visual detection count")
    boxes = []
    identities = set()
    for detection in detections:
        if not isinstance(detection, dict):
            raise ValueError("Invalid visual detection")
        box = detection.get("box")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError("Invalid visual box")
        box = [_number(v, maximum=1.) for v in box]
        if box[2] <= 0 or box[3] <= 0 or box[0] + box[2] > 1.000001 or box[1] + box[3] > 1.000001:
            raise ValueError("Visual box exceeds its image")
        track_id = _integer(detection.get("track_id"), 1, 2**31 - 1)
        if track_id in identities:
            raise ValueError("Visual detections contain duplicate identities")
        identities.add(track_id)
        boxes.append({"box": box, "confidence": _number(detection.get("confidence"), maximum=1.),
                      "track_id": track_id})
    result = {"detections": boxes, "frame_sequence": frame["sequence"], "video_id": frame["video_id"]}
    for key in ("model", "variant"):
        if key in value:
            result[key] = _text(value[key], 120)
    if value.get("inference_ms") is not None:
        result["inference_ms"] = _number(value["inference_ms"], maximum=60_000.)
    return result


class VisualRecorder:
    """One capture at a time; never call wait() on the receiver/event loop."""

    def __init__(self, directory, *, queue_size=8, max_bytes=MAX_BYTES,
                 max_samples=MAX_SAMPLES, max_events=MAX_EVENTS, max_duration=MAX_DURATION):
        self.directory = Path(directory).resolve()
        self.queue_size = _integer(queue_size, 1, MAX_QUEUE)
        self.max_bytes = _integer(max_bytes, RESERVE_BYTES * 2, MAX_BYTES)
        self.max_samples = _integer(max_samples, 1, MAX_SAMPLES)
        self.max_events = _integer(max_events, 1, MAX_EVENTS)
        self.max_duration = _number(max_duration, minimum=.1, maximum=MAX_DURATION)
        self._lock = Lock()
        self._thread = None
        self._queue = Queue(self.queue_size)
        self._stop = Event()
        self._last_at = self._last_sample = None
        self._status = self._empty()

    def _empty(self):
        return {"state": "idle", "id": None, "frames": 0, "samples": 0, "events": 0,
                "dropped": 0, "size_bytes": 0, "max_bytes": self.max_bytes,
                "started_at": None, "ended_at": None, "end_reason": None, "detail": ""}

    @property
    def active(self):
        return self.snapshot()["state"] == "recording"

    def snapshot(self):
        with self._lock:
            return dict(self._status)

    def start(self, identifier, started_at, *, run_id, context=None):
        _identifier(identifier)
        _identifier(run_id)
        started_at = _number(started_at)
        if self._thread is not None and self._thread.is_alive():
            raise ValueError("Visual recording is still recording or finalizing")
        video_age = 1.
        if isinstance(context, dict):
            video_age = context.get("age_limits_s", {}).get("video", 1.)
        video_age = _number(video_age, minimum=.001, maximum=60.)
        self._metadata = {"format": "argos.console.visual", "version": 1, "id": identifier,
            "run_id": run_id, "started_at": started_at, "ended_at": None, "state": "recording",
            "capture_id": uuid4().hex, "video_age_limit_s": video_age, "sample_age_limit_s": SAMPLE_AGE,
            "max_hz": MAX_HZ, "max_bytes": self.max_bytes, "max_samples": self.max_samples,
            "max_events": self.max_events, "max_duration_s": self.max_duration}
        self._queue = Queue(self.queue_size)
        self._stop = Event()
        self._last_at = started_at
        self._last_sample = None
        self._end = None
        with self._lock:
            self._status = {**self._empty(), "state": "recording", "id": identifier, "started_at": started_at}
        self._thread = Thread(target=self._run, name="argos-visual-recorder", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._error("Unable to start visual recording writer")
        return self.snapshot()

    def _clock(self, now):
        now = _number(now)
        if now < self._last_at:
            raise ValueError("Visual receipt clock moved backwards")
        self._last_at = now
        if now - self._metadata["started_at"] >= self.max_duration:
            self.stop(self._metadata["started_at"] + self.max_duration, reason="duration_limit",
                      detail="Visual duration limit reached; media was preserved.")
            return None
        return now

    def _put(self, item, now):
        with self._lock:
            if self._status["state"] != "recording":
                return False
            try:
                self._queue.put_nowait(item)
                return True
            except Full:
                self._status["dropped"] += 1
        self.stop(now, reason="queue_overflow", detail="Visual writer queue filled; media capture ended.")
        return False

    def append(self, now, *, frame=None, vision=None, control=None):
        if not self.active:
            return False
        try:
            now = self._clock(now)
            if now is None:
                return False
            if self._last_sample is not None and now - self._last_sample < 1. / MAX_HZ - 1e-9:
                return False
            self._last_sample = now
            normalized = None
            if frame is not None:
                if not isinstance(frame, dict):
                    raise ValueError("Invalid visual image")
                jpeg = frame.get("jpeg")
                if not isinstance(jpeg, bytes) or not 4 <= len(jpeg) <= MAX_FRAME_BYTES or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                    raise ValueError("Visual image exceeds the JPEG byte limit or is not JPEG")
                received = _number(frame.get("received_at"))
                if received > now:
                    raise ValueError("Visual image receipt is in the future")
                if not max(0., self._metadata["started_at"] - self._metadata["video_age_limit_s"]) <= received <= now:
                    # A much older cached image is not a current observation.
                    frame = None
                else:
                    normalized = {"jpeg": jpeg, "sequence": _integer(frame.get("sequence"), 0, 2**53 - 1),
                        "video_id": _identifier(frame.get("video_id")), "received_at": received,
                        "width": _integer(frame.get("width"), 1, 4096),
                        "height": _integer(frame.get("height"), 1, 4096)}
                    if normalized["width"] * normalized["height"] > 8_388_608:
                        raise ValueError("Visual image exceeds pixel limit")
                    normalized["vision"] = _json(_vision(vision, normalized))
            public = _public(control, top=True) if isinstance(control, dict) else None
            return self._put(("sample", now, normalized, _json(public)), now)
        except (TypeError, ValueError, OverflowError):
            self._error("Invalid visual sample metadata; media capture interrupted")
            return False

    def event(self, now, kind, detail, *, status=None):
        if not self.active:
            return False
        try:
            now = self._clock(now)
            if now is None:
                return False
            kind = _text(kind, 64)
            if re.fullmatch(r"[a-zA-Z0-9_.:-]+", kind) is None:
                raise ValueError("Invalid visual event kind")
            detail = _text(detail, 400)
            status = None if status is None else _text(status, 40)
            return self._put(("event", now, kind, detail, status), now)
        except (TypeError, ValueError, OverflowError):
            self._error("Invalid visual event metadata; media capture interrupted")
            return False

    def stop(self, now, *, reason="stopped", detail=""):
        if not self.active:
            return self.snapshot()
        try:
            now = max(_number(now), self._metadata["started_at"])
            now = min(now, self._metadata["started_at"] + self.max_duration)
            reason, detail = _text(reason, 64), _text(detail, 400)
        except (TypeError, ValueError, OverflowError):
            self._error("Invalid visual finalization metadata")
            return self.snapshot()
        with self._lock:
            if self._status["state"] == "recording":
                self._end = (now, reason, detail)
                self._status.update(state="finalizing", ended_at=now, end_reason=reason, detail=detail)
                self._stop.set()
        return self.snapshot()

    def abort(self, detail="Visual capture interrupted"):
        """Latch optional capture failure without waiting on disk or a join.

        A prior idle/completed capture is not invalidated. The worker observes
        the stop/error flag after its current disk operation and drops queued
        references in its normal finally block.
        """
        detail = _text(detail[:400]) if isinstance(detail, str) else "Visual capture interrupted"
        self._error(detail, only_active=True)
        return self.snapshot()

    def _error(self, detail, *, only_active=False):
        with self._lock:
            if only_active and self._status["state"] not in ("recording", "finalizing"):
                return
            self._status.update(state="error", detail=detail, ended_at=None)
            self._stop.set()

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)
        return self.snapshot()

    def _connect_writer(self, path):
        # Exclusive creation prevents overwriting old media and rejects symlinks.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        db = sqlite3.connect(path, timeout=.2)
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA page_size=4096")
        db.execute(f"PRAGMA max_page_count={(self.max_bytes - RESERVE_BYTES) // 4096}")
        return db

    def _write_sample(self, db, item, counters, previous):
        _, now, frame, control = item
        frame_idx = None
        if frame is not None:
            key = (frame["video_id"], frame["sequence"], frame["received_at"], frame["vision"])
            if previous.get("frame_key") == key:
                frame_idx = previous["frame_idx"]
            else:
                frame_idx = counters["frames"]
                _check_jpeg(frame["jpeg"], frame["width"], frame["height"])
                db.execute("INSERT INTO frames VALUES (?,?,?,?,?,?,?,?,?,?)", (frame_idx, now,
                    frame["received_at"], frame["sequence"], frame["video_id"], frame["width"],
                    frame["height"], frame["vision"], hashlib.sha256(frame["jpeg"]).hexdigest(), frame["jpeg"]))
                counters["frames"] += 1
                previous.update(frame_key=key, frame_idx=frame_idx)
        db.execute("INSERT INTO samples VALUES (?,?,?,?)", (counters["samples"], now, frame_idx, control))
        counters["samples"] += 1
        state = json.loads(control)
        if state is not None:
            framing = state.get("framing") or {}
            vehicle = state.get("vehicle") or {}
            transition = [state.get("phase"), state.get("owned"), state.get("selected_mode"),
                          vehicle.get("mode"), vehicle.get("armed"), framing.get("phase"),
                          framing.get("target_id"), framing.get("reference_height"), framing.get("profile"),
                          framing.get("range_response"),
                          framing.get("reason"), state.get("interruption")]
            if previous.get("transition") != transition and counters["events"] < self.max_events:
                def mode_label(value):
                    if type(value) is not int:
                        return "unavailable"
                    return {0: "Stabilize", 2: "AltHold", 9: "Land"}.get(value, f"#{value}")

                parts = [f"Control {state.get('phase') or 'unknown'}"]
                ownership = state.get("owned")
                parts.append("operator owns control" if ownership is True else
                             "control released" if ownership is False else "ownership unknown")
                parts.append(f"mode {mode_label(state.get('selected_mode'))}")
                if vehicle.get("mode") is not None and vehicle["mode"] != state.get("selected_mode"):
                    parts.append(f"observed mode {mode_label(vehicle['mode'])}")
                parts.append("armed" if vehicle.get("armed") is True else
                             "disarmed" if vehicle.get("armed") is False else "arming state unknown")
                parts.append(f"framing {framing.get('phase') or 'unavailable'}")
                if framing.get("profile") in ("full", "pilot_throttle"):
                    parts.append("pilot throttle" if framing["profile"] == "pilot_throttle" else "full framing")
                if framing.get("range_response") in ("gentle", "normal", "responsive"):
                    parts.append(f"range response {framing['range_response']}")
                if framing.get("target_id") is not None:
                    parts.append(f"person #{framing['target_id']}")
                reference = framing.get("reference_height")
                if type(reference) in (int, float):
                    parts.append(f"reference {reference:.1%}")
                if framing.get("reason"):
                    parts.append(f"reason: {framing['reason']}")
                detail = "; ".join(parts)[:400]
                db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (counters["events"], now,
                    "control_state", detail, "sampled"))
                counters["events"] += 1
                previous["transition"] = transition

    def _run(self):
        db = None
        path = self.directory / f"{self._metadata['id']}.visual.sqlite3"
        counters = {"frames": 0, "samples": 0, "events": 0}
        previous = {}
        payload_bytes = RESERVE_BYTES
        last_written = self._metadata["started_at"]
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            db = self._connect_writer(path)
            for sql in SCHEMA.values():
                db.execute(sql)
            db.execute("INSERT INTO recording VALUES (1,?)", (_json(self._metadata),))
            db.commit()
            while True:
                if self.snapshot()["state"] == "error":
                    return
                try:
                    item = self._queue.get(timeout=.05)
                except Empty:
                    if self._stop.is_set():
                        break
                    continue
                estimate = 8192
                if item[0] == "sample":
                    estimate += len(item[3]) + (len(item[2]["jpeg"]) + len(item[2]["vision"]) if item[2] else 0)
                if payload_bytes + estimate > self.max_bytes - RESERVE_BYTES:
                    self._limit(last_written, "size_limit", "Visual size limit reached; media was preserved.")
                    break
                if item[0] == "sample":
                    self._write_sample(db, item, counters, previous)
                else:
                    db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (counters["events"], *item[1:]))
                    counters["events"] += 1
                db.commit()
                last_written = item[1]
                payload_bytes += estimate
                size = path.stat().st_size
                with self._lock:
                    self._status.update(**counters, size_bytes=size)
                if counters["samples"] >= self.max_samples or counters["events"] >= self.max_events:
                    name = "sample_limit" if counters["samples"] >= self.max_samples else "event_limit"
                    self._limit(last_written, name, "Visual sample/event limit reached; media was preserved.")
                    break
            if self.snapshot()["state"] == "error":
                return
            ended, reason, detail = self._end or (last_written, "stopped", "")
            ended = max(ended, last_written)
            with self._lock:
                dropped = self._status["dropped"]
            metadata = {**self._metadata, **counters, "dropped": dropped, "state": "complete",
                        "ended_at": ended, "end_reason": reason, "detail": detail}
            # Persist the directory entry before committing completion. A failed
            # directory sync therefore leaves a visibly incomplete sidecar.
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            db.execute(f"PRAGMA max_page_count={self.max_bytes // 4096}")
            db.execute("UPDATE recording SET metadata=? WHERE id=1", (_json(metadata),))
            size = db.execute("PRAGMA page_count").fetchone()[0] * 4096
            db.commit()
            db.close()
            db = None
            with self._lock:
                if self._status["state"] != "error":
                    self._status.update(state="complete", **counters, size_bytes=size,
                                        ended_at=ended, end_reason=reason, detail=detail)
        except Exception:
            self._error("Visual storage write/finalization failed; telemetry capture is independent")
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
            while True:
                try:
                    self._queue.get_nowait()
                except Empty:
                    break

    def _limit(self, now, reason, detail):
        with self._lock:
            if self._status["state"] == "error":
                return
            self._end = (now, reason, detail)
            self._status.update(state="finalizing", ended_at=now, end_reason=reason, detail=detail,
                                dropped=self._status["dropped"] + self._queue.qsize() + (reason == "size_limit"))
            self._stop.set()


class VisualArchiveError(ArchiveError):
    """Same HTTP error contract as the independent telemetry archive."""


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class VisualArchive:
    """Read-only, one-file validation cache. Invoke outside the control loop."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self._lock = Lock()
        self._key = self._metadata = self._revision = None

    def _open(self, identifier, started_at, run_id, revision=None):
        try:
            _identifier(identifier)
            _identifier(run_id)
            started_at = _number(started_at)
        except (ValueError, TypeError, OverflowError) as exc:
            raise VisualArchiveError("Invalid visual recording identity", 404) from exc
        path = self.directory / f"{identifier}.visual.sqlite3"
        fd = None
        db = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise VisualArchiveError("Visual recording is not a regular file", 404)
            if not 4096 <= info.st_size <= MAX_BYTES:
                raise VisualArchiveError("Visual recording has an invalid size", 413)
            before = _signature(info)
            # immutable forbids recovery/journal writes. An FD-backed path keeps
            # SQLite attached to this exact inode even if the pathname changes.
            descriptor_path = f"/proc/self/fd/{fd}" if Path("/proc/self/fd").is_dir() else f"/dev/fd/{fd}"
            db = sqlite3.connect(f"file:{descriptor_path}?mode=ro&immutable=1", uri=True, timeout=.1)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_FRAME_BYTES + 2 * MAX_ROW_BYTES)
            deadline = time.monotonic() + 5.
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            key = (identifier, started_at, run_id, before)
            if key != self._key:
                metadata = self._validate(db, identifier, started_at, run_id)
                if info.st_size > metadata["max_bytes"]:
                    raise VisualArchiveError("Visual recording exceeds its recorded size bound", 413)
                digest = hashlib.sha256()
                while chunk := os.read(fd, 256 * 1024):
                    digest.update(chunk)
                self._key, self._metadata, self._revision = key, metadata, digest.hexdigest()
            self._unchanged(path, fd, before)
            if revision is not None and revision != self._revision:
                raise VisualArchiveError("Visual recording changed; reopen this session", 409)
            return path, fd, db, before, deepcopy(self._metadata), self._revision
        except FileNotFoundError:
            if db is not None:
                db.close()
            if fd is not None:
                os.close(fd)
            return None
        except Exception as exc:
            if db is not None:
                db.close()
            if fd is not None:
                os.close(fd)
            if isinstance(exc, VisualArchiveError):
                raise
            raise VisualArchiveError("Invalid or inaccessible visual recording") from exc

    def _unchanged(self, path, fd, before):
        try:
            current = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or _signature(current) != before or _signature(os.fstat(fd)) != before:
                raise VisualArchiveError("Visual recording changed; reopen this session", 409)
        except OSError as exc:
            raise VisualArchiveError("Visual recording changed; reopen this session", 409) from exc

    def _validate(self, db, identifier, start, run_id):
        schema = {name: sql for name, sql in db.execute("SELECT name,sql FROM sqlite_master")}
        if schema != SCHEMA or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise VisualArchiveError("Invalid visual recording schema or database")
        rows = db.execute("SELECT id,metadata FROM recording").fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or len(rows[0][1]) > MAX_ROW_BYTES:
            raise VisualArchiveError("Missing visual recording metadata")
        value = json.loads(rows[0][1])
        keys = {"format", "version", "id", "run_id", "started_at", "ended_at", "state", "capture_id",
                "video_age_limit_s", "sample_age_limit_s", "max_hz", "max_bytes", "max_samples",
                "max_events", "max_duration_s", "frames", "samples", "events", "dropped", "end_reason", "detail"}
        if not isinstance(value, dict) or value.get("state") != "complete":
            raise VisualArchiveError("Visual recording did not complete")
        if set(value) != keys or value["format"] != "argos.console.visual" or type(value["version"]) is not int or value["version"] != 1:
            raise VisualArchiveError("Unsupported visual recording metadata")
        if (value["id"], value["run_id"], value["started_at"]) != (identifier, run_id, start):
            raise VisualArchiveError("Visual recording belongs to a different telemetry capture", 409)
        _identifier(value["capture_id"])
        _number(value["started_at"])
        end = _number(value["ended_at"], minimum=start, maximum=start + MAX_DURATION)
        _number(value["video_age_limit_s"], minimum=.001, maximum=60.)
        if value["max_hz"] != MAX_HZ or value["sample_age_limit_s"] != SAMPLE_AGE:
            raise VisualArchiveError("Unsupported visual sampling policy")
        _integer(value["max_bytes"], RESERVE_BYTES * 2, MAX_BYTES)
        _integer(value["max_samples"], 1, MAX_SAMPLES)
        _integer(value["max_events"], 1, MAX_EVENTS)
        _number(value["max_duration_s"], minimum=.1, maximum=MAX_DURATION)
        if end - start > value["max_duration_s"] + 1e-9:
            raise VisualArchiveError("Invalid visual recording duration")
        _integer(value["dropped"], 0, MAX_SAMPLES + MAX_EVENTS + MAX_QUEUE)
        _text(value["detail"], 400)
        _text(value["end_reason"], 64)
        frames = {}
        previous = start
        for idx, at, received, seq, video_id, width, height, vision, digest, length in db.execute(
                "SELECT idx,available,received,sequence,video_id,width,height,vision,digest,length(jpeg) FROM frames ORDER BY idx"):
            if idx != len(frames) or idx >= MAX_SAMPLES:
                raise VisualArchiveError("Invalid visual frame index")
            _number(at, minimum=previous, maximum=end)
            _number(received, minimum=max(0., start - value["video_age_limit_s"]), maximum=at)
            _integer(seq, 0, 2**53 - 1)
            _identifier(video_id)
            _integer(width, 1, 4096)
            _integer(height, 1, 4096)
            if width * height > 8_388_608:
                raise VisualArchiveError("Invalid visual frame dimensions")
            _integer(length, 4, MAX_FRAME_BYTES)
            if not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None or len(vision) > MAX_ROW_BYTES:
                raise VisualArchiveError("Invalid visual frame digest or metadata")
            parsed = json.loads(vision)
            if parsed != _vision(parsed, {"sequence": seq, "video_id": video_id}):
                raise VisualArchiveError("Invalid matched detection metadata")
            frames[idx] = at
            previous = at
        counts = {"frames": len(frames)}
        previous = start
        count = 0
        for idx, at, frame_idx, control in db.execute("SELECT idx,at,frame_idx,control FROM samples ORDER BY idx"):
            if idx != count or count >= value["max_samples"]:
                raise VisualArchiveError("Invalid visual sample index")
            _number(at, minimum=previous, maximum=end)
            if count and at - previous < 1. / MAX_HZ - 1e-9:
                raise VisualArchiveError("Visual samples exceed the recorded rate bound")
            if frame_idx is not None and (type(frame_idx) is not int or frame_idx not in frames or frames[frame_idx] > at):
                raise VisualArchiveError("Visual sample refers to a missing or future image")
            if len(control) > MAX_ROW_BYTES:
                raise VisualArchiveError("Oversized visual control metadata")
            parsed = json.loads(control)
            if parsed is not None and (not isinstance(parsed, dict) or parsed != _public(parsed, top=True)):
                raise VisualArchiveError("Invalid public control metadata")
            previous, count = at, count + 1
        counts["samples"] = count
        previous = start
        count = 0
        for idx, at, kind, detail, status in db.execute("SELECT idx,at,kind,detail,status FROM events ORDER BY idx"):
            if idx != count or count >= value["max_events"]:
                raise VisualArchiveError("Invalid visual event index")
            _number(at, minimum=previous, maximum=end)
            _text(kind, 64)
            if re.fullmatch(r"[a-zA-Z0-9_.:-]+", kind) is None:
                raise VisualArchiveError("Invalid visual event kind")
            _text(detail, 400)
            if status is not None:
                _text(status, 40)
            previous, count = at, count + 1
        counts["events"] = count
        if any(type(value[key]) is not int or value[key] != count for key, count in counts.items()):
            raise VisualArchiveError("Visual completion counts disagree with recorded data")
        return value

    def _use(self, identifier, started_at, run_id, revision, operation):
        with self._lock:
            opened = self._open(identifier, started_at, run_id, revision)
            if opened is None:
                return {"state": "missing", "id": identifier, "detail": "This session has no visual sidecar"}
            path, fd, db, before, metadata, current_revision = opened
            try:
                result = operation(db, metadata, current_revision, path)
                self._unchanged(path, fd, before)
                return result
            except (sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
                if isinstance(exc, VisualArchiveError):
                    raise
                raise VisualArchiveError("Invalid visual replay data") from exc
            finally:
                db.close()
                os.close(fd)

    def metadata(self, identifier, *, started_at, run_id):
        return self._use(identifier, started_at, run_id, None,
            lambda db, meta, revision, path: {**meta, "revision": revision,
                "duration_s": meta["ended_at"] - meta["started_at"], "size_bytes": path.stat().st_size})

    def replay(self, identifier, offset, *, revision, started_at, run_id):
        try:
            offset = _number(offset)
        except (ValueError, TypeError, OverflowError) as exc:
            raise VisualArchiveError("Invalid visual cursor") from exc
        if not isinstance(revision, str) or re.fullmatch("[0-9a-f]{64}", revision) is None:
            raise VisualArchiveError("A visual revision is required", 409)

        def read(db, meta, rev, path):
            now = meta["started_at"] + offset
            row = db.execute("SELECT idx,at,frame_idx,control FROM samples WHERE at<=? ORDER BY at DESC,idx DESC LIMIT 1", (now,)).fetchone()
            state = "waiting" if row is None else "recent"
            sample = frame = None
            if row is not None:
                idx, at, frame_idx, control = row
                sample = {"index": idx, "at_s": at - meta["started_at"], "control": json.loads(control)}
                if now - at > meta["sample_age_limit_s"]:
                    state = "gap"
                if frame_idx is not None:
                    values = db.execute("SELECT idx,available,received,sequence,video_id,width,height,vision FROM frames WHERE idx=?", (frame_idx,)).fetchone()
                    index, available, received, seq, video, width, height, vision = values
                    analysis = json.loads(vision)
                    age = now - received
                    fresh = age <= meta["video_age_limit_s"] and state == "recent" and now <= meta["ended_at"]
                    frame = {"index": index, "source_sequence": seq, "video_id": video,
                        "at_s": received - meta["started_at"], "available_at_s": available - meta["started_at"],
                        "width": width, "height": height, "age_s": age, "state": "recent" if fresh else "stale",
                        "detections": analysis["detections"] if analysis else [], "vision": analysis}
                    if state == "recent" and not fresh:
                        state = "stale"
            if now > meta["ended_at"]:
                state = "ended"
            events = [{"index": idx, "at_s": at - meta["started_at"], "kind": kind, "detail": detail, "status": status}
                      for idx, at, kind, detail, status in db.execute(
                          "SELECT idx,at,kind,detail,status FROM events WHERE at<=? ORDER BY at DESC,idx DESC LIMIT 50", (now,))]
            before = db.execute("SELECT MAX(at) FROM samples WHERE at<?", (now,)).fetchone()[0]
            after = db.execute("SELECT MIN(at) FROM samples WHERE at>?", (now,)).fetchone()[0]
            event_count = db.execute("SELECT COUNT(*) FROM events WHERE at<=?", (now,)).fetchone()[0]
            return {"state": state, "at_s": offset, "revision": rev, "sample": sample, "frame": frame,
                    "events": list(reversed(events)), "events_count": event_count,
                    "events_limit": 50, "previous_at_s": None if before is None else before - meta["started_at"],
                    "next_at_s": None if after is None else after - meta["started_at"]}
        return self._use(identifier, started_at, run_id, revision, read)

    def framing_report(self, identifier, *, revision, started_at, run_id, duration_s=None):
        """Read an observation summary using the same bound file as replay.

        The query never reads JPEG blobs or re-runs vision/guidance. Validation,
        checksum/revision fencing and the final inode check stay inside _use.
        """
        if not isinstance(revision, str) or re.fullmatch("[0-9a-f]{64}", revision) is None:
            raise VisualArchiveError("A visual revision is required", 409)
        if duration_s is not None:
            try:
                duration_s = _number(duration_s)
            except (ValueError, TypeError, OverflowError) as exc:
                raise VisualArchiveError("Invalid recorded session duration") from exc

        def read(db, meta, rev, path):
            if duration_s is not None:
                if duration_s < meta["ended_at"] - meta["started_at"] - 1e-8:
                    raise VisualArchiveError("Visual recording ends after its journal", 409)
                meta = {**meta, "session_duration_s": max(duration_s, meta["ended_at"] - meta["started_at"])}
            samples = ((index, at, json.loads(control), received)
                       for index, at, control, received in db.execute(
                           "SELECT s.idx,s.at,s.control,f.received FROM samples s "
                           "LEFT JOIN frames f ON f.idx=s.frame_idx ORDER BY s.idx"))
            events = [{"index": index, "at_s": at - meta["started_at"], "kind": kind,
                       "detail": detail, "status": status, "source": "event"}
                      for index, at, kind, detail, status in db.execute(
                          "SELECT idx,at,kind,detail,status FROM events ORDER BY idx DESC LIMIT ?",
                          (MAX_REPORT_EVENTS,))]
            result = framing_report(samples, events, meta)
            result.update(id=identifier, revision=rev)
            return result
        return self._use(identifier, started_at, run_id, revision, read)

    def frame(self, identifier, index, *, revision, started_at, run_id):
        try:
            _integer(index, 0, MAX_SAMPLES - 1)
        except (ValueError, TypeError, OverflowError) as exc:
            raise VisualArchiveError("Invalid archived image index", 404) from exc
        if not isinstance(revision, str) or re.fullmatch("[0-9a-f]{64}", revision) is None:
            raise VisualArchiveError("A visual revision is required", 409)

        def read(db, meta, rev, path):
            row = db.execute("SELECT jpeg,digest,width,height FROM frames WHERE idx=?", (index,)).fetchone()
            if row is None:
                raise VisualArchiveError("Archived image not found", 404)
            jpeg, digest, width, height = row
            if (not isinstance(jpeg, bytes) or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9")
                    or hashlib.sha256(jpeg).hexdigest() != digest):
                raise VisualArchiveError("Archived image failed its digest check")
            try:
                _check_jpeg(jpeg, width, height)
            except (OSError, ValueError, ImportError) as exc:
                raise VisualArchiveError("Archived image failed JPEG validation") from exc
            return jpeg
        result = self._use(identifier, started_at, run_id, revision, read)
        if isinstance(result, dict):
            raise VisualArchiveError("Archived image not found", 404)
        return result

    def completed_path(self, identifier, *, revision, started_at, run_id):
        if not isinstance(revision, str) or re.fullmatch("[0-9a-f]{64}", revision) is None:
            raise VisualArchiveError("A visual revision is required", 409)
        result = self._use(identifier, started_at, run_id, revision, lambda db, meta, rev, path: path)
        if isinstance(result, dict):
            raise VisualArchiveError("Visual recording not found", 404)
        return result

    def open_download(self, identifier, *, revision, started_at, run_id):
        """Return the already-verified inode, rewound; caller owns its close.

        The HTTP iterator must bound bytes to initial fstat size and compare
        fstat identity during reads. No later pathname reopen is necessary.
        """
        if not isinstance(revision, str) or re.fullmatch("[0-9a-f]{64}", revision) is None:
            raise VisualArchiveError("A visual revision is required", 409)
        with self._lock:
            opened = self._open(identifier, started_at, run_id, revision)
            if opened is None:
                raise VisualArchiveError("Visual recording not found", 404)
            path, fd, db, before, metadata, current_revision = opened
            try:
                self._unchanged(path, fd, before)
                os.lseek(fd, 0, os.SEEK_SET)
                stream = os.fdopen(fd, "rb")
                stream.visual_signature = before
                stream.visual_size_bytes = before[2]
                return stream
            except Exception:
                os.close(fd)
                raise
            finally:
                db.close()
