"""Independent virtual pilot input for a same-host ArduPilot SITL receiver.

This sends the native SITL RC UDP protocol: eight or sixteen native-endian
uint16 PWM values. It sends no MAVLink message, heartbeat, or RC override.
Only explicit IPv4 loopback peers are accepted. The CLI keeps sending at 50 Hz
in its own process while another process supplies or stops assistance.

ArduPilot's UDP receiver retains the last values and synthesizes receiver frames
even after UDP stops. ``paused`` and process exit therefore mean UDP silence,
not radio loss. A scenario must separately inject and observe SIM_RC_FAIL=1 to
exercise missing receiver pulses; this module does not change that parameter.
"""
from __future__ import annotations

import argparse
from ipaddress import IPv4Address
import json
import os
import selectors
import socket
import struct
import sys
import threading
import time


DEFAULT_CHANNELS = (1500, 1500, 1100, 1500, 1100, 1100, 1100, 1100)
PWM_MIN, PWM_MAX = 1100, 1900
INTERVAL = .02
MAX_COMMAND_BYTES = 4096


def _peer(value):
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("RC peer must be an (IPv4 loopback literal, port) tuple")
    host, port = value
    if not isinstance(host, str):
        raise ValueError("RC peer needs an IPv4 loopback literal")
    try:
        address = IPv4Address(host)
    except ValueError as exc:
        raise ValueError("RC peer needs an IPv4 loopback literal") from exc
    if not address.is_loopback:
        raise ValueError("The virtual radio is restricted to IPv4 loopback SITL")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("RC peer port must be between 1 and 65535")
    return str(address), port


def _channels(values):
    if not isinstance(values, (list, tuple)) or len(values) not in (8, 16):
        raise ValueError("Provide exactly 8 or 16 PWM channels")
    if any(isinstance(value, bool) or not isinstance(value, int)
           or not PWM_MIN <= value <= PWM_MAX for value in values):
        raise ValueError("PWM channels must be integers between 1100 and 1900")
    return tuple(values)


class SITLRadioTransport:
    """One explicit, simulation-only native receiver peer; opening is passive."""

    def __init__(self, *, peer: tuple[str, int]):
        self.peer = _peer(peer)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._socket.bind(("127.0.0.1", 0))
            self._socket.setblocking(False)
        except Exception:
            self._socket.close()
            raise
        self._closed = False

    def send(self, channels) -> int:
        """Send one native PWM packet; the count is local acceptance, not receipt."""
        values = _channels(channels)
        if self._closed:
            raise RuntimeError("Virtual radio transport is closed")
        packet = struct.pack(f"={len(values)}H", *values)
        written = self._socket.sendto(packet, self.peer)
        if written != len(packet):
            raise OSError("Incomplete native SITL radio datagram")
        return written

    def close(self):
        if not self._closed:
            self._closed = True
            self._socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _Sender:
    """Keep radio timing independent of the stdin command and stdout reader."""

    def __init__(self, transport):
        self.transport = transport
        self.channels = DEFAULT_CHANNELS
        self.paused = False
        self.packets = 0
        self.error = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="sitl-virtual-radio", daemon=True)

    def _run(self):
        while not self.stopped.is_set():
            started = time.monotonic()
            with self.lock:
                try:
                    if not self.paused:
                        self.transport.send(self.channels)
                        self.packets += 1
                except Exception as exc:
                    self.error = str(exc)
                    self.stopped.set()
                    return
            # Never burst to catch up after a delayed scheduler or send.
            self.stopped.wait(max(0., INTERVAL - (time.monotonic() - started)))

    def snapshot(self):
        with self.lock:
            return {"channels": list(self.channels), "paused": self.paused,
                    "packets_sent": self.packets}

    def command(self, value):
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError("Use one of channels, paused, or stop per command")
        if "channels" in value:
            channels = _channels(value["channels"])
            with self.lock:
                self.channels = channels
        elif "paused" in value and isinstance(value["paused"], bool):
            with self.lock:
                self.paused = value["paused"]
        elif "stop" in value and value["stop"] is True:
            self.stopped.set()
        else:
            raise ValueError("Use channels, paused: true/false, or stop: true")

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1.)


def _parse_peer(value):
    try:
        host, port = value.rsplit(":", 1)
        return _peer((host, int(port)))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("Use an IPv4 loopback RC peer such as 127.0.0.1:5501") from exc


def _emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rc-peer", required=True, type=_parse_peer,
                        help="Explicit IPv4 loopback native SITL receiver, e.g. 127.0.0.1:5501")
    args = parser.parse_args(argv)
    with SITLRadioTransport(peer=args.rc_peer) as transport:
        sender = _Sender(transport)
        sender.thread.start()
        try:
            _emit({"event": "ready", "peer": list(args.rc_peer), **sender.snapshot()})
            with selectors.DefaultSelector() as selector:
                selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
                pending = bytearray()
                while not sender.stopped.is_set():
                    if not selector.select(.05):
                        continue
                    data = os.read(sys.stdin.fileno(), MAX_COMMAND_BYTES + 1)
                    if not data:
                        # EOF is explicit lifecycle shutdown, not receiver failure.
                        break
                    pending.extend(data)
                    while b"\n" in pending:
                        raw, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        if len(raw) > MAX_COMMAND_BYTES:
                            _emit({"event": "error", "error": "Command exceeds 4096 bytes"})
                            return 2
                        try:
                            sender.command(json.loads(raw))
                        except (ValueError, UnicodeError) as exc:
                            _emit({"event": "error", "error": str(exc)})
                        else:
                            _emit({"event": "ack", **sender.snapshot()})
                        if sender.stopped.is_set():
                            break
                    if len(pending) > MAX_COMMAND_BYTES:
                        _emit({"event": "error", "error": "Command exceeds 4096 bytes"})
                        return 2
            if sender.error is not None:
                _emit({"event": "error", "error": sender.error})
                return 1
        except KeyboardInterrupt:
            pass
        finally:
            sender.close()
        _emit({"event": "stopped", **sender.snapshot()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
