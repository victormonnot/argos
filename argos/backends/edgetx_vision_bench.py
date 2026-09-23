"""Finite live-preview to Pocket CH32 bench; both radio RF modules must be OFF.

Run as a package module, or beside edgetx_probe.py and vision_bench_source.py
with Python 3.11+ and pyserial==3.5. This is not an aircraft command backend.
"""
from __future__ import annotations

import argparse
import math
import secrets
import sys
import time

if __package__:
    from .edgetx_probe import Lines, ProbeError, open_port
    from .vision_bench_source import PreviewError, PreviewSource, PreviewValue
else:
    from edgetx_probe import Lines, ProbeError, open_port
    from vision_bench_source import PreviewError, PreviewSource, PreviewValue


HELLO = b"ARGOS_USB_VISION_BENCH_V1"
INTERVAL = .1
WRITE_TIMEOUT = .02
TICK = .01
MAX_GAP = .25
ACK_TIMEOUT = .12
SESSION_LIMIT = 30.
MAX_COMMANDS = 300


class VisionBench:
    """A single guarded session. Any error permanently forbids further writes."""

    def __init__(self, port, *, clock=time.monotonic, sleep=time.sleep):
        self.port = port
        self.clock = clock
        self.sleep = sleep
        self.lines = Lines()
        self.session = secrets.token_hex(4)
        self.ready = False
        self.failed = False
        self.finished = False
        self.next_sequence = 1
        self.session_deadline = None
        self.last_sent = None
        self.last_expiry = None
        self.last_time = None
        # A shorter image deadline must never inherit the generic probe's 1 s
        # write timeout. OS/USB delivery and Lua scheduling are still unmeasured.
        self.port.write_timeout = WRITE_TIMEOUT

    def _now(self):
        now = self.clock()
        if (not math.isfinite(now)
                or (self.last_time is not None and now < self.last_time)):
            self.failed = True
            raise ProbeError("invalid or regressing monotonic clock")
        self.last_time = now
        return now

    def _require_ready(self):
        if not self.ready or self.failed or self.finished:
            raise ProbeError("a fresh vision-bench invocation and handshake are required")

    def _wait(self, expected: bytes, timeout: float):
        deadline = self._now() + timeout
        while self._now() < deadline:
            lines = self.lines.feed(self.port.read(256))
            if self._now() >= deadline:
                break
            found = False
            for line in lines:
                if line == expected and not found:
                    found = True
                elif line == HELLO:
                    # Periodic greetings can be coalesced in the same USB
                    # read, including while waiting for the first greeting.
                    continue
                elif line and (expected != HELLO or line.startswith(b"ARGOS_")):
                    raise ProbeError(f"unexpected vision-bench response: {line!r}")
            # A correct ACK followed by an error/duplicate in this batch fails.
            if found:
                return
            self.sleep(min(.005, max(0., deadline - self._now())))
        raise ProbeError(f"timeout waiting for {expected.decode('ascii')}")

    def _drain(self):
        for line in self.lines.feed(self.port.read(256)):
            if line and line != HELLO:
                raise ProbeError(f"unexpected response between commands: {line!r}")

    def _write(self, line):
        packet = (line + "\n").encode("ascii")
        started = self._now()
        if self.port.write(packet) != len(packet):
            raise ProbeError("partial serial write")
        if self._now() - started > WRITE_TIMEOUT:
            raise ProbeError("serial write exceeded its timing budget")

    def connect(self):
        if self.failed or self.ready or self.finished:
            raise ProbeError("start a new invocation for another vision bench")
        try:
            self.port.reset_input_buffer()
            self._wait(HELLO, 5.)
            self.session_deadline = self._now() + SESSION_LIMIT
            self._write(f"ARGOS_VISION_BEGIN {self.session}")
            self._wait(f"ARGOS_VISION_READY {self.session}".encode(), 2.)
            self.ready = True
        except BaseException:
            self.failed = True
            raise

    def pace(self, *, until=None):
        """Pace before fetching the next preview, observing serial expiry."""
        self._require_ready()
        try:
            deadline = self._now() if self.last_sent is None else self.last_sent + INTERVAL
            if until is not None:
                deadline = min(deadline, until)
            while True:
                self._drain()
                now = self._now()
                if now >= deadline:
                    break
                self.sleep(min(.005, deadline - now))
        except BaseException:
            self.failed = True
            raise

    def send(self, proposal: PreviewValue) -> tuple[int, int]:
        self._require_ready()
        try:
            if type(proposal.value) is not int or not -128 <= proposal.value <= 128:
                raise ProbeError("vision output must be an integer from -128 to 128")
            if (type(proposal.deadline) not in (int, float)
                    or not math.isfinite(proposal.deadline)):
                raise ProbeError("invalid preview deadline")
            if self.next_sequence > MAX_COMMANDS:
                raise ProbeError("vision bench is limited to 300 commands")
            # Account for time spent reading the API or queued serial reports.
            self._drain()
            now = self._now()
            if self.last_sent is not None:
                if now - self.last_sent > MAX_GAP:
                    raise ProbeError("command interval exceeded 250 ms")
                if now < self.last_sent + INTERVAL - 1e-9:
                    raise ProbeError("command rate exceeds 10 Hz")
                if now >= self.last_expiry:
                    raise ProbeError("previous command expired; start a new invocation")
            remaining = min(proposal.deadline, self.session_deadline) - now
            # Reserve the entire allowed write time and one radio clock tick.
            # Repeated analysis can never acquire a new image-age deadline.
            ttl = min(20, math.floor((remaining - WRITE_TIMEOUT - TICK) / TICK))
            if ttl < 1:
                limiting = "image" if proposal.deadline <= self.session_deadline else "session"
                raise ProbeError(
                    f"{limiting} deadline too close for another command "
                    f"(command {self.next_sequence}, frame {proposal.frame_sequence}; "
                    f"image remaining {(proposal.deadline - now) * 1000:.1f} ms, "
                    f"session remaining {(self.session_deadline - now) * 1000:.1f} ms; "
                    f"minimum command budget {(WRITE_TIMEOUT + 2 * TICK) * 1000:.1f} ms)"
                )
            sequence = self.next_sequence
            # Keep the last clock observation and deadline checks beside the
            # actual SET write; a generic writer must not obtain a later clock
            # sample and silently reuse a TTL computed before a scheduler pause.
            packet = f"ARGOS_VISION_SET {self.session} {sequence} {proposal.value} {ttl}\n".encode()
            if self.port.write(packet) != len(packet):
                raise ProbeError("partial serial write")
            if self._now() - now > WRITE_TIMEOUT:
                raise ProbeError("serial write exceeded its timing budget")
            self.last_sent = now
            self.last_expiry = now + ttl * TICK
            self._wait(
                f"ARGOS_VISION_ACK {self.session} {sequence} {proposal.value} {ttl}".encode(),
                min(ACK_TIMEOUT, ttl * TICK),
            )
            self.next_sequence += 1
            return sequence, ttl
        except BaseException:
            self.failed = True
            raise

    def finish(self):
        """Send nothing: observe the final expiry, then forbid session reuse."""
        self._require_ready()
        try:
            sequence = self.next_sequence - 1
            if sequence < 1:
                raise ProbeError("no acknowledged command to expire")
            self._wait(f"ARGOS_VISION_IDLE {self.session} {sequence}".encode(), .5)
        except BaseException:
            self.failed = True
            raise
        finally:
            self.finished = True


def run_bridge(source: PreviewSource, bench: VisionBench, duration: int):
    """Require a selection before BEGIN and fetch it again after the handshake."""
    if type(duration) is not int or not 1 <= duration <= 30:
        raise ProbeError("duration must be an integer from 1 to 30 seconds")
    try:
        selected = source.read()
        print(f"Selected person {selected.target_id}; listening for ArgVis (5 seconds).",
              flush=True)
        bench.connect()
        # Leave enough room for a final command's write budget at the radio's
        # independent 30 s ceiling. No SET is used to extend that ceiling.
        end = min(bench._now() + duration, bench.session_deadline - .1)
        while bench._now() < end and bench.next_sequence <= MAX_COMMANDS:
            bench.pace(until=end)
            if bench._now() >= end:
                break
            proposal = source.read()
            if bench._now() >= end:
                break
            sequence, ttl = bench.send(proposal)
            if sequence == 1 or sequence % 5 == 0:
                print(f"ACK {sequence}: CH32 proposal {proposal.value / 1024:+.1%}, "
                      f"frame {proposal.frame_sequence}, TTL {ttl * 10} ms", flush=True)
        bench.finish()
        print(f"Completed {bench.next_sequence - 1} vision exchanges. Lua reports expiry; "
              "confirm CH32 returns to the manual stick. No RF transmission.", flush=True)
    except BaseException:
        # API failures are just as terminal as protocol errors. Never send a
        # cleanup value or retry a session after losing the selected analysis.
        bench.failed = True
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="explicit Pocket USB serial device")
    parser.add_argument("--console-port", type=int, default=8080, help="local ARGOS HTTP port")
    parser.add_argument("--duration", type=int, default=20, help="1..30 seconds, default 20")
    args = parser.parse_args(argv)
    if not args.port.strip() or args.port != args.port.strip():
        parser.error("--port must be a nonempty device without surrounding whitespace")
    if not 1 <= args.console_port <= 65535:
        parser.error("--console-port must be between 1 and 65535")
    if not 1 <= args.duration <= 30:
        parser.error("--duration must be between 1 and 30")
    port = source = None
    try:
        print("Vision-to-CH32 bench only. Use ARGOS VIS, both RF modules OFF, "
              "ArgVis in LUA1, the verified native gate, USB-VCP = Lua and USB Serial. "
              "Select a person in the local ARGOS Yaw preview first. "
              "Any aircraft supplying video must be disarmed with propellers removed.", flush=True)
        source = PreviewSource(port=args.console_port)
        port = open_port(args.port)
        bench = VisionBench(port)
        run_bridge(source, bench, args.duration)
        return 0
    except KeyboardInterrupt:
        print("Vision bench interrupted; sending stopped. Check manual CH32 takeover.",
              file=sys.stderr)
        return 130
    except (PreviewError, ProbeError, OSError, ImportError, ValueError) as exc:
        print(f"Vision bench stopped: {exc}. No retry; check manual CH32 takeover.",
              file=sys.stderr)
        return 1
    finally:
        try:
            if port is not None:
                port.close()
        finally:
            if source is not None:
                source.close()


if __name__ == "__main__":
    raise SystemExit(main())
