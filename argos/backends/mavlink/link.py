"""MAVLink framing on a passive, explicitly configured transport.

All calls use the caller's monotonic run clock. Receipt timestamps describe local
processing, not capture, firmware time, or radio latency. A successful send means
the local transport accepted a complete frame; it proves neither reception nor
application by an autopilot. There is no automatic heartbeat, parameter update,
command queue, retry, arming, or mode change.

Sequence scope is mandatory. Use CHANNEL for one encoder counter across the entire
peer stream; COMPONENT for independent counters per (sysid, compid). Both require
the complete unfiltered stream for each counter. Routed/merged streams without
that property are outside this instrument's loss-measurement contract.

UDP datagrams contain complete frames. Serial/TCP preserve incomplete frames between
polls. A partial write closes the link rather than letting a later message append
to a broken frame. Bytes accepted before that failure remain counted. An I/O
exception with an unknown write count is recorded explicitly, never as zero bytes.
This module implements neither authentication nor vehicle readiness checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import struct
from types import MappingProxyType
from typing import Mapping

from argos.harness.link import LinkSnapshot, LinkStats, SeqPolicy

from .transport import Transport


class SequenceScope(Enum):
    CHANNEL = "channel"
    COMPONENT = "component"


class SendStatus(Enum):
    ACCEPTED = "accepted"    # complete frame accepted by the local transport
    BLOCKED = "blocked"      # zero bytes; nothing queued for a later call
    PARTIAL = "partial"      # known prefix accepted; link closed
    ERROR = "error"          # unknown write count; link closed
    INVALID = "invalid"      # encoding rejected before any I/O or clock mutation
    CLOSED = "closed"


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    bytes_written: int | None
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.status is SendStatus.ACCEPTED


@dataclass(frozen=True)
class Received:
    received_at: float
    system: int
    component: int
    sequence: int
    message_id: int
    type_name: str
    fields: Mapping[str, object]
    frame: bytes


@dataclass(frozen=True)
class LinkReport:
    traffic: LinkSnapshot
    rx_bytes: int             # all local input bytes, including corrupt framing
    bad_bytes: int
    unsupported_frames: int   # dialect cannot validate these; the link is closed
    partial_writes: int
    unknown_writes: int       # traffic TX bytes are only a lower bound when nonzero
    read_errors: int
    invalid_sends: int
    closed: bool
    last_error: str


def _time(value: float) -> float:
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("time must be a finite real number")
    return float(value)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value


class MavlinkLink:
    """Single-owner, threadless MAVLink I/O. Owns and closes its transport.

    Construct the transport explicitly, then pass it here. The same class serves
    UDP, TCP and serial. Calls are in nondecreasing time order, including empty polls
    and reports. A rejected request does not advance that clock.
    """

    def __init__(self, transport: Transport, *, sequence_scope: SequenceScope,
                 system: int = 255, component: int = 190, started_at: float = 0.,
                 window: float = 3., policy: SeqPolicy | None = None):
        if not isinstance(sequence_scope, SequenceScope):
            raise ValueError("choose SequenceScope.CHANNEL or SequenceScope.COMPONENT")
        for name, value in (("system", system), ("component", component)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
                raise ValueError(f"{name} must be an integer in 1..255")
        started_at = _time(started_at)
        stats = LinkStats(window=window, started_at=started_at, policy=policy)
        try:
            from pymavlink.dialects.v20 import ardupilotmega
        except ImportError as exc:
            raise ImportError('install the MAVLink extra: pip install -e ".[mavlink]"') from exc
        self._dialect = ardupilotmega
        self._encoder = ardupilotmega.MAVLink(None, srcSystem=system, srcComponent=component)
        self._decoder = self._new_decoder()
        self._transport = transport
        self._scope = sequence_scope
        self._stats = stats
        self._clock = started_at
        self._closed = False
        self._rx_bytes = self._bad_bytes = 0
        self._unsupported_frames = 0
        self._partial_writes = self._unknown_writes = 0
        self._read_errors = self._invalid_sends = 0
        self._last_error = ""

    def _new_decoder(self):
        decoder = self._dialect.MAVLink(None)
        decoder.robust_parsing = True
        return decoder

    def _checked_time(self, now: float) -> float:
        now = _time(now)
        if now < self._clock:
            raise ValueError("call time predates the last accepted call")
        return now

    def poll(self, now: float, *, max_reads: int = 8) -> tuple[Received, ...]:
        """Consume at most max_reads chunks; never wait for a heartbeat or packet.

        Every decoded message is counted before delivery, including message types
        the application may later ignore. Corrupt bytes are counted separately.
        """
        now = self._checked_time(now)
        if isinstance(max_reads, bool) or not isinstance(max_reads, int) or not 1 <= max_reads <= 64:
            raise ValueError("max_reads must be an integer in 1..64")
        self._clock = now
        received = []
        if self._closed:
            return ()
        for _ in range(max_reads):
            try:
                data = self._transport.read()
            except OSError as exc:
                self._read_errors += 1
                self._fail(str(exc))
                break
            if not data:
                break
            self._rx_bytes += len(data)
            if self._transport.datagram:
                self._decoder = self._new_decoder()
            for message in self._decoder.parse_buffer(data) or ():
                frame = bytes(message.get_msgbuf())
                if message.get_type() == "BAD_DATA":
                    self._bad_bytes += len(frame)
                    continue
                if message.get_msgId() == self._dialect.MAVLINK_MSG_ID_UNKNOWN:
                    # Pymavlink returns UNKNOWN before checking its CRC and leaves
                    # synthetic header defaults on that object. Counting it as a
                    # decoded frame would invent a source and a sequence number.
                    self._unsupported_frames += 1
                    self._fail(f"unsupported message dialect: {message.get_type()}")
                    return tuple(received)
                system, component = message.get_srcSystem(), message.get_srcComponent()
                key = (system, component) if self._scope is SequenceScope.COMPONENT else "channel"
                self._stats.on_rx(now, message.get_seq(), len(frame), source=key)
                received.append(Received(now, system, component, message.get_seq(),
                                         message.get_msgId(), message.get_type(),
                                         _freeze(message.to_dict()), frame))
            if self._transport.datagram:
                # An incomplete datagram cannot be completed by the next datagram.
                self._bad_bytes += self._decoder.buf_len()
                self._decoder = self._new_decoder()
        return tuple(received)

    def send(self, message, now: float, *, force_v1: bool = False) -> SendResult:
        """Encode one message and attempt one write. There is no deferred send."""
        now = self._checked_time(now)
        if self._closed:
            return SendResult(SendStatus.CLOSED, 0)
        if not isinstance(force_v1, bool):
            raise ValueError("force_v1 must be bool")
        try:
            if (not isinstance(message, self._dialect.MAVLink_message)
                    or message.get_msgId() not in self._dialect.mavlink_map):
                raise ValueError("message must belong to the ardupilotmega v2 dialect")
            frame = message.pack(self._encoder, force_mavlink1=force_v1)
        except (ValueError, TypeError, OverflowError, struct.error) as exc:
            self._invalid_sends += 1
            return SendResult(SendStatus.INVALID, 0, str(exc))
        self._clock = now
        try:
            written = self._transport.write(frame)
        except BlockingIOError:
            written = 0
        except OSError as exc:
            self._unknown_writes += 1
            self._fail(str(exc))
            return SendResult(SendStatus.ERROR, None, str(exc))
        if (isinstance(written, bool) or not isinstance(written, int)
                or not 0 <= written <= len(frame)):
            self._unknown_writes += 1
            self._fail("transport returned an invalid write count")
            return SendResult(SendStatus.ERROR, None, self._last_error)
        self._stats.on_tx(now, written)
        if written == 0:
            return SendResult(SendStatus.BLOCKED, 0)
        self._encoder.seq = (self._encoder.seq + 1) % 256
        if written != len(frame):
            self._partial_writes += 1
            self._fail("partial frame written; transport closed")
            return SendResult(SendStatus.PARTIAL, written, self._last_error)
        return SendResult(SendStatus.ACCEPTED, written)

    def report(self, now: float) -> LinkReport:
        now = self._checked_time(now)
        self._clock = now
        return LinkReport(self._stats.snapshot(now), self._rx_bytes, self._bad_bytes,
                          self._unsupported_frames,
                          self._partial_writes, self._unknown_writes, self._read_errors,
                          self._invalid_sends, self._closed, self._last_error)

    def _fail(self, detail: str) -> None:
        self._last_error = detail
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._transport.close()
            except OSError as exc:
                self._last_error = f"{self._last_error}; close: {exc}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
