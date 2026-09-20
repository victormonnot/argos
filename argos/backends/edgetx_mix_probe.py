"""Finite EdgeTX Lua mixer bench with a disconnected aircraft and radio RF off.

Run as a package module, or beside edgetx_probe.py with Python 3.11+ and
pyserial==3.5. The fixed pattern exercises a Lua mixer output and its reported
expiry; it does not establish a flight failsafe or aircraft command path.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time

if __package__:
    from .edgetx_probe import Lines, ProbeError, open_port
else:
    from edgetx_probe import Lines, ProbeError, open_port


HELLO = b"ARGOS_USB_MIX_BENCH_V1"
VALUES = (-256, 0, 256)
INTERVAL = .1


class MixProbe:
    """One finite session; a protocol or I/O failure forbids further writes."""

    def __init__(self, port, *, clock=time.monotonic, sleep=time.sleep):
        self.port = port
        self.clock = clock
        self.sleep = sleep
        self.lines = Lines()
        self.session = secrets.token_hex(4)
        self.ready = False
        self.failed = False
        self.next_sequence = 1
        self.last_ack = None
        self.idle_sequence = None

    def _wait(self, expected: bytes, timeout: float):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            lines = self.lines.feed(self.port.read(256))
            if self.clock() >= deadline:
                break
            found = False
            for line in lines:
                if line == expected and not found:
                    found = True
                elif line == HELLO and expected != HELLO:
                    continue
                elif line.startswith(b"ARGOS_MIX_"):
                    raise ProbeError(f"unexpected mixer response: {line!r}")
                elif expected != HELLO and line:
                    raise ProbeError(f"unexpected serial response: {line!r}")
            # Validate the entire batch before accepting its expected response.
            if found:
                return
            self.sleep(min(.01, max(0., deadline - self.clock())))
        raise ProbeError(f"timeout waiting for {expected.decode('ascii')}")

    def _quiet(self, duration: float):
        """Pace without discarding an early expiry or unsolicited response."""
        deadline = self.clock() + max(0., duration)
        while self.clock() < deadline:
            for line in self.lines.feed(self.port.read(256)):
                if line and line != HELLO:
                    raise ProbeError(f"unexpected response while waiting: {line!r}")
            self.sleep(min(.01, max(0., deadline - self.clock())))

    def _write(self, text: str):
        packet = (text + "\n").encode("ascii")
        if self.port.write(packet) != len(packet):
            raise ProbeError("partial serial write")

    def _require_ready(self):
        if not self.ready or self.failed:
            raise ProbeError("a live mixer-bench handshake is required before sending")

    def connect(self):
        if self.failed or self.ready:
            raise ProbeError("start a new connection for another test")
        try:
            self.port.reset_input_buffer()
            # Even BEGIN waits for an exact greeting from the guarded Lua script.
            self._wait(HELLO, 5.)
            self._write(f"ARGOS_MIX_BEGIN {self.session}")
            self._wait(f"ARGOS_MIX_READY {self.session}".encode("ascii"), 2.)
            self.ready = True
        except BaseException:
            self.failed = True
            raise

    def set_value(self, value: int) -> int:
        self._require_ready()
        if type(value) is not int or value not in VALUES:
            raise ProbeError("the bench permits only -256, 0 or 256")
        if self.next_sequence > 120:
            raise ProbeError("the mixer bench is limited to 120 exchanges")
        sequence = self.next_sequence
        try:
            if self.last_ack is not None:
                self._quiet(INTERVAL - (self.clock() - self.last_ack))
            self._write(f"ARGOS_MIX_SET {self.session} {sequence} {value}")
            self._wait(f"ARGOS_MIX_ACK {self.session} {sequence} {value}".encode("ascii"), 2.)
            self.last_ack = self.clock()
            self.next_sequence += 1
            self.idle_sequence = None
            return sequence
        except BaseException:
            self.failed = True
            raise

    def wait_idle(self) -> int:
        self._require_ready()
        sequence = self.next_sequence - 1
        if not sequence or self.idle_sequence == sequence:
            raise ProbeError("expiry requires a new acknowledged value")
        try:
            self._wait(f"ARGOS_MIX_IDLE {self.session} {sequence}".encode("ascii"), 2.)
            self.idle_sequence = sequence
            return sequence
        except BaseException:
            self.failed = True
            raise

    def hold_idle(self, duration: float):
        self._require_ready()
        if self.idle_sequence != self.next_sequence - 1:
            raise ProbeError("a confirmed expiry is required before the neutral pause")
        try:
            self._quiet(duration)
        except BaseException:
            self.failed = True
            raise


def run_pattern(probe: MixProbe):
    """A fixed 60-exchange pattern, with two explicitly observed expiries."""
    for value, count, label in [(0, 10, "0%"), (256, 20, "+25%")]:
        print(f"Phase {label}: watch CH32 in the radio channel monitor.", flush=True)
        for _ in range(count):
            sequence = probe.set_value(value)
        print(f"ACK {sequence}/60: {label} phase acknowledged.", flush=True)
    print("Expiry phase: sending stopped; CH32 should return to 0%.", flush=True)
    sequence = probe.wait_idle()
    print(f"IDLE 1/2 after ACK {sequence}: Lua reports expired output = 0%.", flush=True)
    probe.hold_idle(.5)
    for value, count, label in [(-256, 20, "-25%"), (0, 10, "0%")]:
        print(f"Phase {label}: watch CH32 in the radio channel monitor.", flush=True)
        for _ in range(count):
            sequence = probe.set_value(value)
        print(f"ACK {sequence}/60: {label} phase acknowledged.", flush=True)
    sequence = probe.wait_idle()
    print(f"IDLE 2/2 after ACK {sequence}: Lua reports expired output = 0%.", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--port", help="explicit Pocket USB serial device")
    mode.add_argument("--list-ports", action="store_true", help="list metadata without opening devices")
    args = parser.parse_args(argv)
    if args.port is not None and (not args.port.strip() or args.port != args.port.strip()):
        parser.error("--port must be a nonempty device without surrounding whitespace")
    port = None
    try:
        if args.list_ports:
            from serial.tools.list_ports import comports
            for info in sorted(comports(), key=lambda item: item.device):
                print(json.dumps({"port": info.device, "description": info.description,
                                  "vid": info.vid, "pid": info.pid}))
            return 0
        print("Pocket mixer bench only. Leave the aircraft disconnected. Use model "
              "ARGOS USB with both RF modules OFF, ArgMix on CH32, USB-VCP = Lua "
              "and USB Serial selected.", flush=True)
        port = open_port(args.port)
        probe = MixProbe(port)
        print("Listening for ArgMix (5 seconds); no data sent yet...", flush=True)
        probe.connect()
        print("ArgMix greeting and session handshake received.", flush=True)
        run_pattern(probe)
        print("Completed 60 mixer exchanges and 2 expiry reports. Confirm CH32 visually; "
              "this is not a flight failsafe validation.", flush=True)
        return 0
    except ImportError:
        print('Install serial support: python -m pip install "pyserial==3.5"', file=sys.stderr)
        return 1
    except (OSError, ProbeError, ValueError) as exc:
        print(f"Mixer bench stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Mixer bench interrupted; no cleanup command sent.", file=sys.stderr)
        return 130
    finally:
        if port is not None:
            port.close()


if __name__ == "__main__":
    raise SystemExit(main())
