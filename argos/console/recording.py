"""User-controlled telemetry journals and optional visual sidecars; no emission."""
from pathlib import Path
from uuid import uuid4
import json

from argos.backends.mavlink import RecordingWriter
from argos.backends.mavlink.recording import MAX_CONTEXT_BYTES, MAX_END_DETAIL_BYTES, MAX_LINE_BYTES
from .recording_limits import MAX_BYTES, MAX_EVENTS
from .views import safe_text
from .visual_recording import VisualRecorder


def _bounded_detail(value):
    """Keep arbitrary transport error text inside the checksummed end record."""
    value = safe_text(value)[:MAX_END_DETAIL_BYTES]
    if len(json.dumps(value).encode("ascii")) <= MAX_END_DETAIL_BYTES:
        return value
    while len(json.dumps(value + "…").encode("ascii")) > MAX_END_DETAIL_BYTES:
        value = value[:-1]
    return value + "…"


class ConsoleRecorder:
    """Single owner, borrowed by successive console sessions.

    Successful journals stay on disk across process restarts. This object owns
    the current recording status; RecordingArchive exposes earlier local files.
    """

    def __init__(self, directory: Path, *, max_events=MAX_EVENTS, max_bytes=MAX_BYTES):
        if type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS:
            raise ValueError(f"max_events must be between 1 and {MAX_EVENTS}")
        # Room for the largest header/context and a complete end/checksum even
        # when using a smaller bound in an embedding application or a test.
        if type(max_bytes) is not int or not MAX_CONTEXT_BYTES + 3 * MAX_LINE_BYTES <= max_bytes <= MAX_BYTES:
            raise ValueError("max_bytes must fit the journal metadata and remain within the archive limit")
        self.directory = Path(directory).resolve()
        self.max_events, self.max_bytes = max_events, max_bytes
        self._writer = self._stream = None
        self.visual = VisualRecorder(self.directory)
        self._visual_enabled = False
        self._visual_error = ""
        self._now = 0.
        self._path = None
        self._status = {"state": "idle", "id": None, "events": 0,
                        "started_at": None, "ended_at": None, "error": "", "download_url": None,
                        "end_reason": None, "end_detail": "", "size_bytes": 0}

    @property
    def active(self):
        return self._status["state"] == "recording"

    def snapshot(self):
        result = {**self._status, "max_events": self.max_events, "max_bytes": self.max_bytes}
        if self._writer is not None:
            result["size_bytes"] = self._writer.bytes_written
        if self._visual_enabled:
            result["visual"] = self.visual.snapshot()
            if self._visual_error:
                result["visual"] = {**result["visual"], "state": "error",
                    "id": self._status["id"], "detail": self._visual_error}
        return result

    def start(self, now, *, context=None, include_visual=False):
        if self.active:
            raise ValueError("A recording is already in progress")
        if type(include_visual) is not bool:
            raise ValueError("include_visual must be a boolean")
        if self.visual.snapshot()["state"] in ("recording", "finalizing"):
            raise ValueError("Wait for the previous visual recording to finish")
        if include_visual and (not isinstance(context, dict) or not context.get("run_id")):
            raise ValueError("Visual recording requires the capture session context")
        self._now = now
        self._visual_enabled = include_visual
        self._visual_error = ""
        identifier = uuid4().hex
        self._status = {"state": "recording", "id": identifier, "events": 0,
                        "started_at": now, "ended_at": None, "error": "", "download_url": None,
                        "end_reason": None, "end_detail": "", "size_bytes": 0}
        self._path = self.directory / f"{identifier}.jsonl"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._stream = self._path.open("xb")
            self._writer = RecordingWriter(self._stream, started_at=now, context=context, with_completion=True)
        except Exception as exc:
            self.fail(f"Unable to create recording: {exc}")
        if self.active and include_visual:
            try:
                self.visual.start(identifier, now, run_id=context["run_id"], context=context)
            except Exception:
                self._visual_error = "Unable to start visual recording; telemetry capture continues"
        return self.snapshot()

    def append(self, event):
        if self.active:
            self._now = event.received_at
            try:
                # Every rx/end/checksum line is bounded by MAX_LINE_BYTES. Stop
                # before accepting a frame that could consume the footer space.
                if self._writer.bytes_written + 3 * MAX_LINE_BYTES > self.max_bytes:
                    self.stop(event.received_at, reason="size_limit",
                              detail="Size limit reached; the recording was preserved.")
                    return
                self._writer.append(event)
                self._status["events"] += 1
                if self._status["events"] >= self.max_events:
                    self.stop(event.received_at, reason="event_limit",
                              detail=f"Limit of {self.max_events} messages reached; the recording was preserved.")
            except Exception as exc:
                self.fail(f"Recording write interrupted: {exc}")

    def _stop_visual(self, now, *, reason, detail):
        if self._visual_enabled and not self._visual_error:
            try:
                self.visual.stop(now, reason=reason, detail=detail)
            except Exception:
                self._visual_error = "Unable to finalize visual recording; telemetry capture is independent"
                try:
                    self.visual.abort(self._visual_error)
                except Exception:
                    pass

    def fail(self, detail):
        self._stop_visual(self._now, reason="telemetry_error", detail="Telemetry journal could not be finalized")
        if self._writer is not None:
            self._status["size_bytes"] = self._writer.bytes_written
        self._status.update(state="error", error=_bounded_detail(detail), ended_at=None, download_url=None)
        self._writer = None
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def stop(self, now, *, reason="stopped", detail=""):
        if not self.active:
            raise ValueError("No recording in progress")
        self._now = now
        self._stop_visual(now, reason=reason, detail=detail)
        try:
            detail = _bounded_detail(detail)
            self._writer.finish(now, reason=reason, detail=detail)
            self._status["size_bytes"] = self._writer.bytes_written
            self._stream.flush()
            self._stream.close()
            self._stream = self._writer = None
            self._status.update(state="complete", ended_at=now, end_reason=reason, end_detail=detail,
                download_url=f"/api/recordings/{self._status['id']}/download")
        except Exception as exc:
            self.fail(f"Unable to finalize recording: {exc}")
        return self.snapshot()

    def completed_path(self, identifier):
        if self._status["state"] != "complete" or identifier != self._status["id"]:
            return None
        return self._path
