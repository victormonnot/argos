"""Continuous, bounded yaw assistance transport for the ArgFly mixer script.

This first implementation is for software validation and a subsequent propeller-
removed bench. It is not a flight validation. Radio configuration and native
mixer gates are required separately. Run with ``python -m ... --port DEVICE``.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import re
import secrets
import sys
import threading
import time

from .edgetx_probe import Lines, ProbeError, open_port
from .yaw_stream_source import YawDemand, YawSource


HELLO = b"ARGOS_YAW_STREAM_V1"
INTERVAL = .1
WRITE_TIMEOUT = .02
HEALTH_TIMEOUT = 1.
READ_BUDGET = 4
MAX_SEQUENCE = 2**31 - 1
# EdgeTX Lua 5.3 LUA_32BITS keeps nonnegative integer counters exact to 31 bits.
COUNTER_MODULUS = 2**31
_STATUS = re.compile(rb"AY1 ([0-9a-f]{8}) ([0-9]{1,10}) ([0-9]{1,10}) ([0-9]{1,10}) ([MTAF])")


@dataclass(frozen=True)
class SourceSample:
    """One immutable result; a blocked producer cannot block the radio loop."""

    demand: YawDemand | None
    completed_at: float
    error: bool = False


class SourceWorker:
    """One reader, one overwrite-only mailbox; never a queue of old analyses."""

    def __init__(self, source, *, clock=time.monotonic, interval=.025):
        self.source = source
        self.clock = clock
        self.interval = interval
        self._lock = threading.Lock()
        self._sample = SourceSample(None, clock(), True)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="argos-yaw-source", daemon=True)

    def start(self):
        self._thread.start()

    def snapshot(self):
        with self._lock:
            return self._sample

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    sample = SourceSample(self.source.read(), self.clock())
                except Exception:
                    sample = SourceSample(None, self.clock(), True)
                with self._lock:
                    self._sample = sample
                self._stop.wait(self.interval)
        finally:
            self.source.close()

    def close(self):
        self._stop.set()
        # A broken HTTP implementation must not prevent serial shutdown.
        self._thread.join(timeout=.2)


@dataclass(frozen=True)
class RadioStatus:
    session: str
    generation: int
    ticket: int
    ack: int
    state: str


def parse_status(line: bytes) -> RadioStatus:
    match = _STATUS.fullmatch(line)
    if match is None:
        raise ProbeError("invalid yaw-stream radio status")
    session, generation, ticket, ack, state = match.groups()
    generation, ticket, ack = int(generation), int(ticket), int(ack)
    if generation >= COUNTER_MODULUS or ticket >= COUNTER_MODULUS or ack > MAX_SEQUENCE:
        raise ProbeError("out-of-range yaw-stream radio status")
    return RadioStatus(session.decode(), generation, ticket, ack, state.decode())


def _advances(value, previous):
    """Nonnegative int31 counters wrap; duplicate/old reports confer no freshness."""
    return 0 < (value - previous) % COUNTER_MODULUS < COUNTER_MODULUS // 2


class YawStream:
    """One USB connection; no SET acknowledgement blocks its 10 Hz sender.

    Tickets are radio-issued capabilities with a radio-side 300 ms lifetime.
    Late queued commands cannot acquire a new lifetime on receipt. This bound
    is not an end-to-end guarantee that output expires at an image's deadline:
    local image deadlines forbid new transmissions, and the existing lease
    expires separately on the radio's callback clock.
    """

    def __init__(self, port, *, clock=time.monotonic, nonce=lambda: secrets.token_hex(4)):
        self.port = port
        self.clock = clock
        self.nonce = nonce
        self.lines = Lines()
        self.port.timeout = 0
        self.port.write_timeout = WRITE_TIMEOUT
        self.started_at = self.last_time = clock()
        self.greeted = False
        self.session = None
        self.status = None
        self.last_status_at = None
        self.last_ack_at = None
        self.pending_since = None
        self.sent_sequence = 0
        self.next_send_at = self.started_at
        self.session_started_at = self.started_at
        self.selection_key = None
        self.source_bad_since = None
        self.source_fault = False
        self.pending_begin = False
        self.failed = False
        self.reason = "waiting for ArgFly greeting"
        self._backpressure_since = None
        self._reset_without_session = False

    def _now(self):
        now = self.clock()
        if not math.isfinite(now) or now < self.last_time:
            raise ProbeError("invalid or regressing monotonic clock")
        self.last_time = now
        return now

    def _rotate(self, now, reason):
        previous_session = self.session
        self.session = self.nonce()
        if (not isinstance(self.session, str)
                or not re.fullmatch(r"[0-9a-f]{8}", self.session)
                or self.session in ("00000000", previous_session)):
            raise ProbeError("invalid session nonce")
        self.status = None
        self.last_status_at = None
        self.last_ack_at = None
        self.pending_since = None
        self.sent_sequence = 0
        self.session_started_at = now
        self.next_send_at = now
        self.pending_begin = True
        self.reason = reason + "; SC middle then SC up required"

    def _receive(self, now):
        for _ in range(READ_BUDGET):
            chunk = self.port.read(256)
            if not chunk:
                break
            for line in self.lines.feed(chunk):
                if not line:
                    continue
                if line == HELLO:
                    if not self.greeted:
                        self.greeted = True
                        self._rotate(now, "radio connected")
                    continue
                # An unrelated serial device gets no writes before our greeting.
                if not self.greeted:
                    continue
                status = parse_status(line)
                if status.session == "00000000":
                    if not self._reset_without_session:
                        self._rotate(now, "radio session reset")
                        self._reset_without_session = True
                    continue
                if status.session != self.session or self.pending_begin:
                    continue
                previous = self.status
                if previous is not None:
                    if status.generation == previous.generation:
                        if not _advances(status.ticket, previous.ticket):
                            continue
                        if status.ack < previous.ack:
                            raise ProbeError("radio acknowledgement regressed")
                    elif not _advances(status.generation, previous.generation):
                        continue
                if status.ack > self.sent_sequence:
                    raise ProbeError("radio acknowledged a command never sent")
                self._reset_without_session = False
                self.last_status_at = now
                if previous is None or status.generation != previous.generation:
                    self.pending_since = None
                    self.last_ack_at = now
                elif status.ack > previous.ack:
                    self.last_ack_at = now
                    self.pending_since = None if status.ack == self.sent_sequence else now
                if status.state not in ("T", "A"):
                    self.pending_since = None
                self.status = status
        # Drain bounded chunks over later ticks; never send against an old
        # status while a backlog remains to be read.
        return getattr(self.port, "in_waiting", 0) == 0

    def _write(self, packet, now):
        if getattr(self.port, "out_waiting", 0):
            if self._backpressure_since is None:
                self._backpressure_since = now
            if now - self._backpressure_since >= HEALTH_TIMEOUT:
                raise ProbeError("serial output stalled; reconnect requires a new SC cycle")
            return False
        self._backpressure_since = None
        started = self._now()
        written = self.port.write(packet)
        if written != len(packet) or self._now() - started > WRITE_TIMEOUT:
            raise ProbeError("serial write was partial or exceeded its bounded timeout")
        return True

    def _source(self, sample, now):
        if (not isinstance(sample, SourceSample)
                or not math.isfinite(sample.completed_at) or sample.completed_at > now):
            raise ProbeError("invalid source mailbox timestamp")
        bad = sample.error or sample.demand is None or now - sample.completed_at >= HEALTH_TIMEOUT
        if bad:
            if self.source_bad_since is None:
                self.source_bad_since = min(now, sample.completed_at)
            if now - self.source_bad_since >= HEALTH_TIMEOUT and not self.source_fault:
                self.source_fault = True
                if self.greeted:
                    self._rotate(now, "source unavailable")
            return None
        self.source_bad_since = None
        self.source_fault = False
        demand = sample.demand
        if (type(demand.value) is not int or not -128 <= demand.value <= 128
                or type(demand.valid) is not bool or not math.isfinite(demand.deadline)
                or (demand.selection_key is not None and not isinstance(demand.selection_key, tuple))):
            raise ProbeError("invalid source demand")
        if demand.selection_key != self.selection_key:
            self.selection_key = demand.selection_key
            if self.greeted:
                self._rotate(now, "selected target changed")
        return demand

    def step(self, sample: SourceSample):
        """One short nonblocking iteration; caller polls independently of HTTP."""
        if self.failed:
            raise ProbeError("failed USB connection must be closed")
        try:
            now = self._now()
            drained = self._receive(now)
            demand = self._source(sample, now)
            if not self.greeted:
                if now - self.started_at >= 5.:
                    raise ProbeError("timeout waiting for ARGOS_YAW_STREAM_V1")
                return
            if self.pending_begin:
                # A non-command prefix invalidates a partial previous input.
                if drained and self._write(f"#\nAB1 {self.session}\n".encode(), now):
                    self.pending_begin = False
                    self.session_started_at = now
                return
            last_health = (self.session_started_at if self.last_status_at is None
                           else self.last_status_at)
            if now - last_health >= HEALTH_TIMEOUT:
                self._rotate(now, "radio status stopped advancing")
                return
            if self.status is None:
                self.reason = "waiting for radio session status"
                return
            if self.pending_since is not None and now - self.pending_since >= HEALTH_TIMEOUT:
                self._rotate(now, "radio acknowledgements stopped advancing")
                return
            state = self.status.state
            if state not in ("T", "A"):
                self.reason = "manual: SC middle then SC up required" if state == "M" else "radio fault: return SC to middle"
                return
            if not drained or now < self.next_send_at:
                return
            if self.sent_sequence >= MAX_SEQUENCE:
                self._rotate(now, "sequence exhausted")
                return
            # Recheck the clock beside the bounded write, including after I/O.
            now = self._now()
            valid = bool(demand is not None and demand.valid and demand.selection_key is not None
                         and demand.deadline > now and not self.source_fault)
            value = demand.value if valid else 0
            seq = self.sent_sequence + 1
            packet = (f"AS1 {self.session} {self.status.generation} {self.status.ticket} "
                      f"{seq} {int(valid)} {value}\n").encode()
            if self._write(packet, now):
                self.sent_sequence = seq
                self.next_send_at = self._now() + INTERVAL  # no catch-up bursts
                if self.pending_since is None:
                    self.pending_since = now
                if not valid:
                    self.reason = "manual requested: target temporarily unavailable"
                elif state == "A" and self.status.ack > 0:
                    self.reason = "radio reports assistance active"
                else:
                    self.reason = "correction requested: waiting for radio acceptance"
        except BaseException:
            self.failed = True
            raise


def run_stream(device, source, *, duration=None, opener=open_port, clock=time.monotonic,
               sleep=time.sleep, report=print):
    """Keep the source worker alive across USB reconnects; every connection rearms."""
    worker = SourceWorker(source, clock=clock)
    worker.start()
    started = clock()
    port = stream = None
    reconnect_at = started
    previous_reason = None
    try:
        while duration is None or clock() - started < duration:
            now = clock()
            if stream is None and now >= reconnect_at:
                try:
                    port = opener(device)
                    port.reset_input_buffer()
                    stream = YawStream(port, clock=clock)
                except (OSError, ProbeError):
                    if port is not None:
                        port.close()
                    port = None
                    reconnect_at = now + 1.
            if stream is not None:
                try:
                    stream.step(worker.snapshot())
                    reason = stream.reason
                except (OSError, ProbeError) as exc:
                    reason = f"USB stopped: {exc}; waiting to reconnect, manual control required"
                    port.close()
                    port = stream = None
                    reconnect_at = clock() + 1.
                if reason != previous_reason:
                    report(reason)
                    previous_reason = reason
            sleep(.005)
    finally:
        if port is not None:
            port.close()
        worker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="explicit Pocket serial device")
    parser.add_argument("--console-port", type=int, default=8080)
    parser.add_argument("--duration", type=float, help="optional finite software/bench run in seconds")
    args = parser.parse_args(argv)
    if not args.port.strip() or args.port != args.port.strip():
        parser.error("--port must be nonempty without surrounding whitespace")
    if not 1 <= args.console_port <= 65535:
        parser.error("--console-port must be between 1 and 65535")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("--duration must be a positive finite number")
    print("ArgFly yaw stream: software/propeller-removed bench implementation. "
          "A new connection requires SC middle then SC up; only yaw assistance is sent.", flush=True)
    try:
        run_stream(args.port, YawSource(port=args.console_port), duration=args.duration,
                   report=lambda text: print(text, flush=True))
    except KeyboardInterrupt:
        print("Sending stopped. Return SC to middle; radio leases will expire.", file=sys.stderr)
        return 130
    except (ImportError, OSError, ValueError, ProbeError) as exc:
        print(f"Yaw stream stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
