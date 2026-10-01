"""Own the continuous serial worker separately from the HTTP event loop."""
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

from argos.backends.edgetx_yaw_stream import run_stream
from argos.backends.yaw_stream_source import YawSource


class YawAssistService:
    def __init__(self, device, console_port, *, manifest=None, directory=None,
                 runner=run_stream, source_factory=YawSource, clock=time.monotonic,
                 recovery_log_capacity=1024):
        if type(recovery_log_capacity) is not int or recovery_log_capacity < 1:
            raise ValueError("Recovery log capacity must be a positive integer")
        self.device, self.console_port = device, console_port
        self.manifest = manifest or {}
        self.directory = Path(directory) if directory is not None else None
        self.runner, self.source_factory, self.clock = runner, source_factory, clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._at = None
        self._status = {"connected": False, "reason": "Waiting for Pocket", "radio_state": None}
        self._last_write = None
        self._last_transition = None
        self._log_error = None
        self._log_item = None
        self._log_thread = None
        self._log_wake = threading.Event()
        self._log_stop = threading.Event()
        self._log_dropped = 0
        self._recovery_capacity = recovery_log_capacity
        self._recovery_items = deque()
        self._recovery_inflight = 0
        self._recovery_queued = 0
        self._recovery_written = 0
        self._recovery_dropped = 0
        self._recovery_failed = 0
        self._recovery_dirty = False
        self._local_source = None
        self._status_sinks = []
        self._status_sink_errors = 0

    def add_status_sink(self, sink):
        """Attach a bounded nonblocking diagnostic consumer; never another USB reader."""
        if not callable(sink):
            raise TypeError("Status sink must be callable")
        with self._lock:
            if sink not in self._status_sinks:
                self._status_sinks.append(sink)

    def remove_status_sink(self, sink):
        with self._lock:
            if sink in self._status_sinks:
                self._status_sinks.remove(sink)

    def bind_console(self, session, loop):
        """Use owner-published demands in the integrated app; CLI keeps HTTP."""
        if self._thread is not None:
            raise RuntimeError("Bind the console before starting yaw assistance")
        if self.source_factory is YawSource:
            from .yaw_source import LocalYawSource
            self._local_source = LocalYawSource(session, loop.call_soon_threadsafe, clock=self.clock)

    def publish_source(self):
        if self._local_source is not None:
            self._local_source.publish()

    def _recovery_status_locked(self):
        pending = len(self._recovery_items) + self._recovery_inflight
        return dict(queued=self._recovery_queued, written=self._recovery_written,
                    dropped=self._recovery_dropped, failed=self._recovery_failed,
                    pending=pending, capacity=self._recovery_capacity,
                    shutdown_incomplete=self._log_stop.is_set() and pending > 0)

    def _start_logger_locked(self):
        # Called under the short mailbox lock: the HTTP and serial producers
        # cannot create two writers racing over the same files.
        if self._log_thread is None:
            thread = threading.Thread(target=self._log_loop, name="argos-yaw-log", daemon=True)
            try:
                thread.start()
            except RuntimeError as exc:
                self._log_error = f"Cannot start session logger: {exc}"
            else:
                self._log_thread = thread
        self._log_wake.set()

    def record_recovery(self, event):
        """Queue every recovery attempt without doing disk I/O on its caller.

        Unlike sampled bridge status, recovery events are an ordered FIFO: no
        deduplication or replacement. The explicit dropped counter is the only
        permitted overflow behavior; disk failure is reported separately.
        """
        if self.directory is None:
            return False
        if not isinstance(event, dict):
            raise TypeError("Recovery event must be a dictionary")
        record = {"monotonic_at": self.clock(), "event": deepcopy(event)}
        with self._lock:
            self._recovery_dirty = True
            if (self._stop.is_set()
                    or len(self._recovery_items) + self._recovery_inflight >= self._recovery_capacity):
                self._recovery_dropped += 1
                reason = "service is stopping" if self._stop.is_set() else "queue is full"
                self._log_error = (f"Recovery log {reason}; "
                                   f"{self._recovery_dropped} event(s) not recorded")
                self._log_wake.set()
                return False
            self._recovery_items.append(record)
            self._recovery_queued += 1
            self._start_logger_locked()
        return True

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Yaw service already started")
        self._thread = threading.Thread(target=self._run, name="argos-yaw-stream", daemon=True)
        self._thread.start()

    def _publish(self, value):
        now = self.clock()
        with self._lock:
            self._status, self._at = deepcopy(value), now
            sinks = tuple(self._status_sinks)
        if sinks:
            from .filming import assistance_view
            observation = {"host_monotonic_at": now, **deepcopy(value)}
            observation.update(assistance_view(observation, now))
            for sink in sinks:
                try:
                    sink(observation)
                except Exception:
                    # Recording failures do not change flight commands/leases.
                    with self._lock:
                        self._status_sink_errors += 1
        if self.directory is None:
            return
        transition = (value.get("connected"), value.get("radio_state"),
                      value.get("radio_cause"), value.get("reason"))
        if (transition == self._last_transition and self._last_write is not None
                and now - self._last_write < 1.):
            return
        self._last_write, self._last_transition = now, transition
        with self._lock:
            if self._log_item is not None:
                self._log_dropped += 1
            self._log_item = {"monotonic_at": now, **deepcopy(value),
                              "log_samples_replaced": self._log_dropped}
            self._start_logger_locked()

    def _write_log(self, record):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            text = json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n"
            temporary = self.directory / "status.json.tmp"
            temporary.write_text(text)
            temporary.replace(self.directory / "status.json")
            with (self.directory / "events.jsonl").open("a") as output:
                output.write(text)
        except (OSError, TypeError, ValueError) as exc:
            with self._lock:
                self._log_error = str(exc)

    def _write_recovery_log(self, records):
        self.directory.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n"
                       for record in records)
        with (self.directory / "recovery.jsonl").open("a") as output:
            output.write(text)

    def _write_recovery_status(self):
        with self._lock:
            summary = {**self._recovery_status_locked(), "log_error": self._log_error}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.directory / "recovery-status.json.tmp"
            temporary.write_text(json.dumps(summary, allow_nan=False, separators=(",", ":")) + "\n")
            temporary.replace(self.directory / "recovery-status.json")
        except OSError as exc:
            with self._lock:
                self._log_error = f"Cannot write recovery log summary: {exc}"

    def _log_loop(self):
        # Status may replace older samples; recovery attempts never do. Both
        # have bounded storage and share this sole disk writer.
        while True:
            self._log_wake.wait()
            self._log_wake.clear()
            while True:
                with self._lock:
                    item, self._log_item = self._log_item, None
                    recovery = [self._recovery_items.popleft()
                                for _ in range(min(64, len(self._recovery_items)))]
                    self._recovery_inflight = len(recovery)
                    dirty, self._recovery_dirty = self._recovery_dirty, False
                    stopping = self._log_stop.is_set()
                if item is None and not recovery and not dirty:
                    if stopping:
                        return
                    break
                if item is not None:
                    self._write_log(item)
                if recovery:
                    try:
                        self._write_recovery_log(recovery)
                    except (OSError, TypeError, ValueError) as exc:
                        with self._lock:
                            self._recovery_failed += len(recovery)
                            self._log_error = f"Cannot write recovery log: {exc}"
                    else:
                        with self._lock:
                            self._recovery_written += len(recovery)
                    finally:
                        with self._lock:
                            self._recovery_inflight = 0
                if recovery or dirty:
                    self._write_recovery_status()

    def _run(self):
        try:
            source = self._local_source or self.source_factory(port=self.console_port)
            options = ({"worker_factory": lambda source, **kw: source}
                       if self._local_source is not None else {})
            self.runner(self.device, source,
                        stop_event=self._stop, on_status=self._publish,
                        report=lambda message: print(message, flush=True), **options)
        except Exception as exc:
            self._publish({"connected": False, "radio_state": None,
                           "reason": f"Stream stopped: {type(exc).__name__}: {exc}"})

    def snapshot(self):
        with self._lock:
            value, at = deepcopy(self._status), self._at
            recovery_log = self._recovery_status_locked()
            log_error = self._log_error
            sink_errors = self._status_sink_errors
        age = None if at is None else max(0., self.clock() - at)
        value.update(enabled=True, status_age_s=age, manifest=self.manifest,
                     log_error=log_error, recovery_log=recovery_log,
                     recording_sink_errors=sink_errors)
        if at is None or age >= 1.5 or self._stop.is_set():
            value.update(connected=False, radio_state=None,
                         reason="Stream stopped" if self._stop.is_set() else "Waiting for stream status")
        from .filming import assistance_view
        value.update(assistance_view(value, self.clock()))
        return value

    def close(self):
        self._stop.set()
        if self._local_source is not None:
            self._local_source.close()
        if self._thread is not None:
            self._thread.join(timeout=2.)
        self._log_stop.set()
        with self._lock:
            self._recovery_dirty = True
        self._log_wake.set()
        if self._log_thread is not None:
            self._log_thread.join(timeout=.2)
