"""Finite, read-only Betaflight USB diagnostics (MSP API 1.48).

Run with ``python -m argos.backends.betaflight_probe --help``. This module also
runs as a standalone file with only pyserial installed. No console integration,
reconnection, CLI entry, configuration writes or flight commands are provided.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import IntEnum
from functools import reduce
import json
import math
import operator
import os
import struct
import sys
import time


class ReadCommand(IntEnum):
    API_VERSION = 1
    FC_VARIANT = 2
    FC_VERSION = 3
    BOARD_INFO = 4
    RC = 105
    ATTITUDE = 108
    BOXIDS = 119
    STATUS_EX = 150


class ProbeError(Exception):
    """Stop this connection; replies cannot safely be retried after a timeout."""


def encode_request(command: ReadCommand) -> bytes:
    """Only allow enumerated, empty read requests, never arbitrary MSP commands."""
    if not isinstance(command, ReadCommand):
        raise ValueError("expected an allowlisted ReadCommand")
    return b"$M<" + bytes((0, command, command))


@dataclass(frozen=True)
class Reply:
    command: int
    payload: bytes
    error: bool


class ReplyParser:
    """Bounded MSPv1 response framing; fragmentation and leading noise allowed."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[Reply]:
        if len(data) > 4096:
            raise ProbeError("serial read exceeded 4096 bytes")
        self.buffer.extend(data)
        replies = []
        while self.buffer:
            start = self.buffer.find(b"$M")
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer[-1:] == b"$" else b""
                break
            del self.buffer[:start]
            if len(self.buffer) < 3:
                break
            if self.buffer[2] not in (ord(">"), ord("!")):
                del self.buffer[0]
                continue
            if len(self.buffer) < 5:
                break
            length, command = self.buffer[3:5]
            if length == 255:
                raise ProbeError("unsupported MSP jumbo reply")
            total = length + 6
            if len(self.buffer) < total:
                break
            packet = bytes(self.buffer[:total])
            del self.buffer[:total]
            if reduce(operator.xor, packet[3:-1], 0) != packet[-1]:
                raise ProbeError("MSP reply checksum mismatch")
            replies.append(Reply(command, packet[5:-1], packet[2] == ord("!")))
        return replies


def _need(payload: bytes, minimum: int, label: str) -> None:
    if len(payload) < minimum:
        raise ProbeError(f"truncated {label} reply: {len(payload)} < {minimum}")


def _ascii(payload: bytes) -> str:
    if any(c < 32 or c > 126 for c in payload):
        raise ProbeError("non-printable identity string")
    return payload.decode("ascii")


def _string(payload: bytes, offset: int) -> tuple[str, int]:
    _need(payload, offset + 1, "identity string")
    end = offset + 1 + payload[offset]
    _need(payload, end, "identity string")
    return _ascii(payload[offset + 1:end]), end


def decode_status(payload: bytes, box_ids: tuple[int, ...]) -> dict:
    _need(payload, 16, "STATUS_EX")
    extra = payload[15] & 15
    _need(payload, 25 + extra, "STATUS_EX")
    cycle, errors, sensors, modes = struct.unpack_from("<HHHI", payload)
    # BOXIDS page zero gives at most the first 32 dynamic mode indices.
    armed = bool(modes & (1 << box_ids.index(0))) if 0 in box_ids else None
    return {
        "armed": armed,
        "cycle_time_us": cycle,
        "i2c_errors": errors,
        "sensor_mask": sensors,
        "mode_flags": modes,
        "extra_mode_flags_hex": payload[16:16 + extra].hex(),
        "active_mode_ids_page0": [mode for bit, mode in enumerate(box_ids) if modes & (1 << bit)],
        "pid_profile": payload[10],
        "system_load_percent": struct.unpack_from("<H", payload, 11)[0],
        "pid_profile_count": payload[13],
        "rate_profile": payload[14],
        "arming_disable_flag_count": payload[16 + extra],
        "arming_disable_flags": struct.unpack_from("<I", payload, 17 + extra)[0],
        "configuration_state": payload[21 + extra],
        "core_temperature_c": struct.unpack_from("<H", payload, 22 + extra)[0],
        "rate_profile_count": payload[24 + extra],
    }


def decode_attitude(payload: bytes) -> dict:
    _need(payload, 6, "ATTITUDE")
    roll, pitch, yaw = struct.unpack_from("<hhh", payload)
    return {"roll_deg": roll / 10, "pitch_deg": pitch / 10, "yaw_deg": yaw}


def decode_rc(payload: bytes) -> dict:
    if len(payload) < 8 or len(payload) % 2 or len(payload) > 36:
        raise ProbeError("RC reply needs 4 to 18 complete channels")
    channels = struct.unpack("<" + "H" * (len(payload) // 2), payload)
    # MSP_RC exposes rcData AFTER mapping/failsafe, in internal AERT order.
    return dict(zip(("roll", "pitch", "yaw", "throttle"), channels[:4]),
                aux=list(channels[4:]))


class Probe:
    """One request at a time on an already open nonblocking serial port.

    The caller owns the port and must give it a finite write timeout. On any
    request error the probe becomes unusable: MSPv1 has no transaction sequence
    to distinguish a late reply from a reply to a repeated command.
    """

    def __init__(self, port, *, timeout: float = 1., clock=time.monotonic, sleep=time.sleep):
        if not math.isfinite(timeout) or not .05 <= timeout <= 5:
            raise ValueError("timeout must be between 0.05 and 5 seconds")
        self.port = port
        self.timeout = timeout
        self.clock = clock
        self.sleep = sleep
        self.started = clock()
        self.failed = False
        self.box_ids: tuple[int, ...] | None = None

    def request(self, command: ReadCommand) -> tuple[bytes, float]:
        packet = encode_request(command)
        if self.failed:
            raise ProbeError("probe failed; close this connection before another attempt")
        deadline = self.clock() + self.timeout
        parser = ReplyParser()
        try:
            # Only this process owns the connection. Discard pre-request backlog.
            self.port.reset_input_buffer()
            if self.port.write(packet) != len(packet):
                raise ProbeError("partial serial write; connection must be closed")
            while self.clock() < deadline:
                replies = parser.feed(self.port.read(4096))
                received = self.clock()
                if received >= deadline:
                    break
                for reply in replies:
                    if reply.command != command:
                        continue
                    if reply.error:
                        raise ProbeError(f"controller rejected {command.name}")
                    return reply.payload, received - self.started
                self.sleep(min(.005, max(0., deadline - self.clock())))
            raise ProbeError(f"timeout waiting for {command.name}; exit CLI and disconnect Betaflight Configurator")
        except Exception:
            self.failed = True
            raise

    def identity(self) -> dict:
        api, _ = self.request(ReadCommand.API_VERSION)
        if api != bytes((0, 1, 48)):
            raise ProbeError(f"unsupported MSP API {list(api)}; this probe supports protocol 0 / API 1.48")
        variant, _ = self.request(ReadCommand.FC_VARIANT)
        if variant != b"BTFL":
            raise ProbeError("controller does not identify as Betaflight")
        version, _ = self.request(ReadCommand.FC_VERSION)
        _need(version, 4, "FC_VERSION")
        version_string, _ = _string(version, 3)
        board, _ = self.request(ReadCommand.BOARD_INFO)
        _need(board, 8, "BOARD_INFO")
        target, offset = _string(board, 8)
        name, offset = _string(board, offset)
        manufacturer, _ = _string(board, offset)
        boxes, received = self.request(ReadCommand.BOXIDS)
        if not boxes or len(boxes) > 32 or len(set(boxes)) != len(boxes) or 0 not in boxes:
            raise ProbeError("BOXIDS page zero must identify ARM unambiguously")
        self.box_ids = tuple(boxes)
        return {
            "type": "identity", "received_after_s": round(received, 6),
            "api": "1.48", "variant": "BTFL", "firmware": version_string,
            "firmware_numbers": [2000 + version[0], version[1], version[2]],
            "board_identifier": _ascii(board[:4]),
            "hardware_revision": struct.unpack_from("<H", board, 4)[0],
            "target": target, "board_name": name, "manufacturer": manufacturer,
            "box_ids_page0": list(boxes),
        }

    def sample(self) -> dict:
        if self.box_ids is None:
            raise ProbeError("read and verify identity before sampling")
        sample = {"type": "sample"}
        for name, command, decode in (
            ("status", ReadCommand.STATUS_EX, lambda p: decode_status(p, self.box_ids)),
            ("attitude", ReadCommand.ATTITUDE, decode_attitude),
            ("rc", ReadCommand.RC, decode_rc),
        ):
            payload, received = self.request(command)
            sample[name] = dict(decode(payload), received_after_s=round(received, 6))
        return sample


def open_port(device: str, *, timeout: float):
    import serial

    # Fixed normal MSP speed; deliberately no 1200-baud bootloader trigger.
    port = serial.Serial(port=None, baudrate=115200, timeout=0,
                         write_timeout=timeout, exclusive=True if os.name == "posix" else None)
    try:
        port.dtr = False
        port.rts = False
        port.port = device
        port.open()
    except BaseException:
        port.close()
        raise
    return port


def _show(record: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(record, allow_nan=False), flush=True)
    elif record["type"] == "identity":
        print(f"Betaflight {record['firmware']} | {record['board_name']} | MSP {record['api']}", flush=True)
    elif record["type"] == "sample":
        status, attitude, rc = (record[key] for key in ("status", "attitude", "rc"))
        state = "ARMED" if status["armed"] else "DISARMED"
        print(f"{rc['received_after_s']:6.2f}s {state} | "
              f"angles R/P/Y {attitude['roll_deg']:.1f}/{attitude['pitch_deg']:.1f}/{attitude['yaw_deg']:.0f} deg | "
              f"RC R/P/Y/T {rc['roll']}/{rc['pitch']}/{rc['yaw']}/{rc['throttle']} "
              f"AUX {rc['aux']} | arming blockers 0x{status['arming_disable_flags']:08x}", flush=True)
    elif record["type"] == "end":
        print(f"Completed {record['samples']} samples.", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--port", help="explicit serial device; never auto-selected")
    mode.add_argument("--list-ports", action="store_true", help="list serial metadata without opening devices")
    parser.add_argument("--samples", type=int, default=20, help="1..300 samples (default: 20)")
    parser.add_argument("--interval", type=float, default=.5, help="minimum pause between samples, 0.2..10 seconds")
    parser.add_argument("--timeout", type=float, default=1., help="per-request timeout, 0.05..5 seconds")
    parser.add_argument("--json", action="store_true", help="emit JSON Lines instead of a human-readable display")
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 300:
        parser.error("--samples must be between 1 and 300")
    if not math.isfinite(args.interval) or not .2 <= args.interval <= 10:
        parser.error("--interval must be between 0.2 and 10 seconds")
    if not math.isfinite(args.timeout) or not .05 <= args.timeout <= 5:
        parser.error("--timeout must be between 0.05 and 5 seconds")
    if args.port is not None and (not args.port.strip() or args.port != args.port.strip()):
        parser.error("--port must be a nonempty device without surrounding whitespace")
    port = None
    try:
        if args.list_ports:
            from serial.tools.list_ports import comports
            for info in sorted(comports(), key=lambda p: p.device):
                print(json.dumps({"port": info.device, "description": info.description,
                                  "vid": info.vid, "pid": info.pid}))
            return 0
        print("USB bench readout only. Exit CLI and disconnect Configurator first. "
              "RC values are processed controller values, not proof of a live radio link.", file=sys.stderr)
        port = open_port(args.port, timeout=args.timeout)
        probe = Probe(port, timeout=args.timeout)
        _show(probe.identity(), as_json=args.json)
        for index in range(args.samples):
            _show(probe.sample(), as_json=args.json)
            if index + 1 < args.samples:
                time.sleep(args.interval)
        _show({"type": "end", "samples": args.samples}, as_json=args.json)
        return 0
    except ImportError:
        print('Install serial support: python -m pip install "pyserial==3.5"', file=sys.stderr)
        return 1
    except (OSError, ProbeError, ValueError) as exc:
        print(f"Probe stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Probe interrupted; no flight command sent.", file=sys.stderr)
        return 130
    finally:
        if port is not None:
            port.close()


if __name__ == "__main__":
    raise SystemExit(main())
