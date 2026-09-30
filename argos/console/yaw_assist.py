"""Own the continuous serial worker separately from the HTTP event loop."""
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

from argos.backends.edgetx_yaw_stream import run_stream
from argos.backends.yaw_stream_source import YawSource


class YawAssistService:
    def __init__(self, device, console_port, *, manifest=None, directory=None,
                 runner=run_stream, source_factory=YawSource, clock=time.monotonic):
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

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Yaw service already started")
        self._thread = threading.Thread(target=self._run, name="argos-yaw-stream", daemon=True)
        self._thread.start()

    def _publish(self, value):
        now = self.clock()
        with self._lock:
            self._status, self._at = deepcopy(value), now
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
        if self._log_thread is None:
            self._log_thread = threading.Thread(target=self._log_loop, name="argos-yaw-log", daemon=True)
            self._log_thread.start()
        self._log_wake.set()

    def _write_log(self, record):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            text = json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n"
            temporary = self.directory / "status.json.tmp"
            temporary.write_text(text)
            temporary.replace(self.directory / "status.json")
            with (self.directory / "events.jsonl").open("a") as output:
                output.write(text)
        except OSError as exc:
            self._log_error = str(exc)

    def _log_loop(self):
        # Disk work can stall without holding the USB sender or accumulating an
        # unbounded event queue. Cumulative radio counters survive overwritten
        # log samples; this is sampled status, not a lossless serial capture.
        while True:
            self._log_wake.wait()
            self._log_wake.clear()
            with self._lock:
                item, self._log_item = self._log_item, None
            if item is not None:
                self._write_log(item)
            if self._log_stop.is_set():
                with self._lock:
                    item, self._log_item = self._log_item, None
                if item is not None:
                    self._write_log(item)
                return

    def _run(self):
        try:
            self.runner(self.device, self.source_factory(port=self.console_port),
                        stop_event=self._stop, on_status=self._publish,
                        report=lambda message: print(message, flush=True))
        except Exception as exc:
            self._publish({"connected": False, "radio_state": None,
                           "reason": f"Stream stopped: {type(exc).__name__}: {exc}"})

    def snapshot(self):
        with self._lock:
            value, at = deepcopy(self._status), self._at
        age = None if at is None else max(0., self.clock() - at)
        value.update(enabled=True, status_age_s=age, manifest=self.manifest,
                     log_error=self._log_error)
        if at is None or age >= 1.5 or self._stop.is_set():
            value.update(connected=False, radio_state=None,
                         reason="Stream stopped" if self._stop.is_set() else "Waiting for stream status")
        return value

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.)
        self._log_stop.set()
        self._log_wake.set()
        if self._log_thread is not None:
            self._log_thread.join(timeout=.2)
