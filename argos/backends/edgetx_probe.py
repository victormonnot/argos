"""Finite USB display test for the EdgeTX ArgosUSB.lua tool, without RC commands.

This file also runs standalone with Python 3.11+ and pyserial==3.5. The host
listens for the tool's greeting before transmitting fixed, numbered pings.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


READY = b"ARGOS_USB_DISPLAY_V1"


class ProbeError(Exception):
    """The display exchange failed; close this connection."""


class Lines:
    """Bound newline framing; never accept a suffix of an oversized line."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        lines = []
        for value in data:
            if value == 10:
                lines.append(bytes(self.buffer).removesuffix(b"\r"))
                self.buffer.clear()
            else:
                self.buffer.append(value)
                if len(self.buffer) > 64:
                    raise ProbeError("oversized serial line; check the selected port and USB-VCP mode")
        return lines


class DisplayProbe:
    def __init__(self, port, *, clock=time.monotonic, sleep=time.sleep):
        self.port = port
        self.clock = clock
        self.sleep = sleep
        self.lines = Lines()
        self.ready = False
        self.failed = False
        self.next_sequence = 1

    def _wait(self, expected: bytes, timeout: float):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            lines = self.lines.feed(self.port.read(256))
            if self.clock() >= deadline:
                break
            for line in lines:
                if line == expected:
                    return
                if expected != READY and line.startswith(b"ARGOS_USB_ACK "):
                    raise ProbeError("unexpected acknowledgement; start a new test")
            self.sleep(min(.01, max(0., deadline - self.clock())))
        raise ProbeError(f"timeout waiting for {expected.decode('ascii')}")

    def connect(self):
        if self.failed or self.ready:
            raise ProbeError("start a new connection for another test")
        try:
            self.port.reset_input_buffer()
            # No bytes are written until the display tool announces itself.
            self._wait(READY, 5.)
            self.ready = True
        except Exception:
            self.failed = True
            raise

    def ping(self) -> int:
        if not self.ready or self.failed:
            raise ProbeError("a live display-tool greeting is required before sending")
        if self.next_sequence > 120:
            raise ProbeError("the display test is limited to 120 pings")
        sequence = self.next_sequence
        try:
            packet = f"ARGOS_USB_PING {sequence}\n".encode("ascii")
            if self.port.write(packet) != len(packet):
                raise ProbeError("partial serial write")
            self._wait(f"ARGOS_USB_ACK {sequence}".encode("ascii"), 2.)
            self.next_sequence += 1
            return sequence
        except Exception:
            self.failed = True
            raise


def open_port(device: str):
    import serial

    port = serial.Serial(port=None, baudrate=115200, timeout=0,
                         write_timeout=1., exclusive=True if os.name == "posix" else None)
    try:
        port.dtr = False
        port.rts = False
        port.port = device
        port.open()
    except BaseException:
        port.close()
        raise
    return port


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--port", help="explicit Pocket USB serial device")
    mode.add_argument("--list-ports", action="store_true", help="list metadata without opening devices")
    parser.add_argument("--samples", type=int, default=20, help="1..120 pings, default 20")
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 120:
        parser.error("--samples must be between 1 and 120")
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
        print("Pocket display test only. Leave the aircraft disconnected. "
              "Open ArgosUSB on the radio with USB-VCP = Lua and USB Serial selected.", flush=True)
        port = open_port(args.port)
        probe = DisplayProbe(port)
        print("Listening for ArgosUSB (5 seconds); no data sent yet...", flush=True)
        probe.connect()
        print("ArgosUSB greeting received.", flush=True)
        for index in range(args.samples):
            sequence = probe.ping()
            print(f"ACK {sequence}/{args.samples}: radio script received the PC message", flush=True)
            if index + 1 < args.samples:
                time.sleep(.5)
        print(f"Completed {args.samples} display exchanges. No RC command sent.", flush=True)
        return 0
    except ImportError:
        print('Install serial support: python -m pip install "pyserial==3.5"', file=sys.stderr)
        return 1
    except (OSError, ProbeError, ValueError) as exc:
        print(f"Display test stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Display test interrupted.", file=sys.stderr)
        return 130
    finally:
        if port is not None:
            port.close()


if __name__ == "__main__":
    raise SystemExit(main())
