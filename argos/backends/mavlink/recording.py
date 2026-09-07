"""Versioned, offline journals of decoded MAVLink receptions.

The wire bytes and their local reception times are authoritative. Decoded fields
are reconstructed, not serialized, so a CRC-valid NaN survives recording and can
reproduce a telemetry-cache rejection. A journal is not a raw transport capture:
it cannot reconstruct discarded bytes, application processing delays, packet loss
before recording, transmission metrics, or the physical age of a measurement.

Binary JSONL uses a header, one ``rx`` line per frame, an explicit ``end`` record
with time/count, and a ``checksum`` footer over ALL preceding bytes, including
the end record and line endings. Version 1 has no capture context. Version 2
requires one bounded ``context`` object immediately after its header; the rest
of the wire-frame and completion records are unchanged. Version 3 additionally
stores an explicit closure reason/detail in the checksummed end record. A valid
closure can describe an interrupted observation; it does not imply a successful
mission. Versions 1 and 2 remain readable and their default writers unchanged.
The digest detects accidental corruption; it is not authentication. Loading
checks the entire journal and EOF before returning any events. A configurable
event limit and line limits bound memory consumption (16 KiB for the context
record only, 1 KiB for every other record).

The writer borrows its stream and does not close or flush it. Call ``finish``
explicitly on successful completion. A partial/failed write poisons the writer:
it cannot append a misleading success footer. This module opens no transport,
sends no messages and performs no wall-clock waiting.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import hashlib
from importlib.metadata import version
import json
import math
import re
from typing import BinaryIO

from .link import Received, _freeze, _time


MAX_LINE_BYTES = 1024
MAX_CONTEXT_BYTES = 16 * 1024
MAX_CONTEXT_DEPTH = 8
MAX_CONTEXT_ITEMS = 1024
MAX_FRAME_BYTES = 280
MAX_END_DETAIL_BYTES = 512  # JSON-escaped ASCII, including the string quotes.
END_REASONS = frozenset({"stopped", "transport_error", "event_limit", "size_limit", "shutdown"})
_HEADER_KEYS = {"kind", "format", "version", "dialect", "codec_version", "clock", "started_at"}


class RecordingError(ValueError):
    """An invalid, incomplete, out-of-order, or unsuccessfully written journal."""


@dataclass(frozen=True)
class Recording:
    started_at: float
    ended_at: float
    codec_version: str
    events: tuple[Received, ...]
    context: Mapping | None = None
    end_reason: str | None = None
    end_detail: str = ""


def _completion(reason, detail):
    if type(reason) is not str or reason not in END_REASONS:
        raise RecordingError("unsupported recording closure reason")
    if type(detail) is not str or len(json.dumps(detail).encode("ascii")) > MAX_END_DETAIL_BYTES:
        raise RecordingError("recording closure detail must be a bounded string")
    try:
        detail.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RecordingError("recording closure detail must be valid Unicode") from exc
    return reason, detail


def normalize_context(value: Mapping) -> dict:
    """Copy a bounded JSON object without coercing arbitrary Python objects.

    Context is optional provenance, independent of any consumer's schema. Its
    depth/node bounds apply before serialization; its complete JSONL record has
    a separate byte bound. Applications validate their own schema after load.
    """
    remaining = MAX_CONTEXT_ITEMS

    def visit(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_CONTEXT_DEPTH:
            raise RecordingError("context exceeds its item or nesting limit")
        if item is None or type(item) is bool:
            return item
        if type(item) is str:
            if len(item) > MAX_CONTEXT_BYTES:
                raise RecordingError("context string exceeds the size limit")
            return item
        if type(item) is int:
            # Avoid enormous integer conversions before the encoded size check.
            if item.bit_length() > MAX_CONTEXT_BYTES * 4:
                raise RecordingError("context integer exceeds the size limit")
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise RecordingError("context numbers must be finite")
            return item
        if isinstance(item, Mapping):
            if len(item) > remaining:
                raise RecordingError("context exceeds its item limit")
            result = {}
            for key, member in item.items():
                if type(key) is not str or len(key) > MAX_CONTEXT_BYTES:
                    raise RecordingError("context keys must be bounded strings")
                result[key] = visit(member, depth + 1)
            return result
        if type(item) in (list, tuple):
            if len(item) > remaining:
                raise RecordingError("context exceeds its item limit")
            return [visit(member, depth + 1) for member in item]
        raise RecordingError("context contains a non-JSON value")

    if not isinstance(value, Mapping):
        raise RecordingError("context must be a JSON object")
    result = visit(value, 0)
    try:
        _encode({"kind": "context", "data": result}, MAX_CONTEXT_BYTES)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise RecordingError(f"invalid context: {exc}") from exc
    return result


def _encode(value, limit):
    line = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
    if len(line) > limit:
        raise RecordingError("record exceeds the line size limit")
    return line


def _stamp(value: object, minimum: float) -> float:
    try:
        value = _time(value)
    except ValueError as exc:
        raise RecordingError(str(exc)) from exc
    if value < minimum:
        raise RecordingError("timestamp precedes the journal origin or last reception")
    return value


class _Codec:
    def __init__(self):
        try:
            from pymavlink.dialects.v20 import ardupilotmega
        except ImportError as exc:
            raise ImportError('install the MAVLink extra: pip install -e ".[mavlink]"') from exc
        self.dialect = ardupilotmega

    def decode(self, frame: bytes, received_at: float) -> Received:
        if not isinstance(frame, bytes) or not 8 <= len(frame) <= MAX_FRAME_BYTES:
            raise RecordingError("frame must be 8..280 bytes")
        decoder = self.dialect.MAVLink(None)
        decoder.robust_parsing = True
        try:
            messages = decoder.parse_buffer(frame) or []
        except (self.dialect.MAVError, ValueError, TypeError) as exc:
            raise RecordingError(f"invalid MAVLink frame: {exc}") from exc
        if len(messages) != 1 or decoder.buf_len() != 0:
            raise RecordingError("record must contain exactly one complete MAVLink frame")
        message = messages[0]
        if (message.get_type() == "BAD_DATA"
                or message.get_msgId() == self.dialect.MAVLINK_MSG_ID_UNKNOWN
                or bytes(message.get_msgbuf()) != frame):
            raise RecordingError("record contains corrupt, unknown or extra MAVLink bytes")
        return Received(received_at, message.get_srcSystem(), message.get_srcComponent(),
                        message.get_seq(), message.get_msgId(), message.get_type(),
                        _freeze(message.to_dict()), frame)


class RecordingWriter:
    """Append original ``Received`` events, before application filtering.

    Their headers must agree with the wire frame. ``fields`` is intentionally
    ignored: the original encoded payload, including non-finite values, is what
    an offline reader will decode. Validation errors leave the stream and clock
    untouched and may be corrected by the caller. I/O failure is terminal.

    Use a fresh binary stream, preferably a file opened with ``xb`` to avoid
    replacing an earlier recording. Caller controls flushing/disk durability.
    Omitting context preserves version 1 byte for byte. A supplied JSON object
    selects version 2 and is validated completely before the first write.
    with_completion=True selects version 3 (an empty context if none supplied)
    and stores closure reason/detail, independent of data integrity.
    """

    def __init__(self, stream: BinaryIO, *, started_at: float = 0., context: Mapping | None = None,
                 with_completion: bool = False):
        if type(with_completion) is not bool:
            raise RecordingError("with_completion must be a boolean")
        started_at = _stamp(started_at, 0.)
        if with_completion and context is None:
            context = {}
        context = normalize_context(context) if context is not None else None
        self._codec = _Codec()
        self._stream = stream
        self._clock = started_at
        self._count = 0
        self._bytes_written = 0
        self._with_completion = with_completion
        self._digest = hashlib.sha256()
        self._finished = self._failed = False
        self._write({
            "kind": "header", "format": "argos.mavlink.rx",
            "version": 3 if with_completion else 1 if context is None else 2,
            "dialect": "ardupilotmega", "codec_version": version("pymavlink"),
            "clock": "local_receive", "started_at": started_at,
        })
        if context is not None:
            self._write({"kind": "context", "data": context}, limit=MAX_CONTEXT_BYTES)

    @property
    def bytes_written(self) -> int:
        """Bytes of successfully written records, excluding a possible partial write."""
        return self._bytes_written

    def _active(self) -> None:
        if self._failed or self._finished:
            raise RecordingError("writer failed or was already finalized")

    def _write(self, value: dict, *, limit: int = MAX_LINE_BYTES) -> None:
        self._active()
        line = _encode(value, limit)
        try:
            written = self._stream.write(line)
        except (OSError, ValueError, TypeError) as exc:
            self._failed = True
            raise RecordingError(f"journal write failed: {exc}") from exc
        if isinstance(written, bool) or not isinstance(written, int) or written != len(line):
            self._failed = True
            raise RecordingError("journal write was incomplete or returned an invalid count")
        self._digest.update(line)
        self._bytes_written += len(line)

    def append(self, event: Received) -> None:
        self._active()
        if not isinstance(event, Received):
            raise RecordingError("append requires a decoded Received event")
        received_at = _stamp(event.received_at, self._clock)
        decoded = self._codec.decode(event.frame, received_at)
        for name in ("system", "component", "sequence", "message_id"):
            value = getattr(event, name)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value != getattr(decoded, name)):
                raise RecordingError(f"{name} disagrees with the recorded frame")
        if not isinstance(event.type_name, str) or event.type_name != decoded.type_name:
            raise RecordingError("type_name disagrees with the recorded frame")
        self._write({"kind": "rx", "received_at": received_at, "frame_hex": event.frame.hex()})
        self._clock = received_at
        self._count += 1

    def finish(self, ended_at: float, *, reason: str | None = None, detail: str = "") -> None:
        """Write a success footer at the declared end of observation, including silence.

        No automatic finalization occurs on stream closure or object destruction.
        An unfinalized journal is incomplete, even if every recorded frame is valid.
        """
        self._active()
        ended_at = _stamp(ended_at, self._clock)
        end = {"kind": "end", "ended_at": ended_at, "events": self._count}
        if self._with_completion:
            reason, detail = _completion("stopped" if reason is None else reason, detail)
            end.update(reason=reason, detail=detail)
        elif reason is not None or detail != "":
            raise RecordingError("closure metadata requires a version 3 writer")
        self._write(end)
        self._write({"kind": "checksum", "sha256": self._digest.hexdigest()})
        self._finished = True


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RecordingError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise RecordingError(f"non-finite JSON metadata: {value}")


def _line(stream: BinaryIO, number: int, *, limit=MAX_LINE_BYTES) -> tuple[bytes, dict]:
    raw = stream.readline(limit + 1)
    if not isinstance(raw, bytes):
        raise RecordingError("journal must be read from a binary stream")
    if not raw:
        raise RecordingError(f"line {number}: journal ended before its completion footer")
    if len(raw) > limit or not raw.endswith(b"\n"):
        raise RecordingError(f"line {number}: oversized or unterminated record")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RecordingError(f"line {number}: invalid JSON record: {exc}") from exc
    if not isinstance(value, dict):
        raise RecordingError(f"line {number}: record must be a JSON object")
    return raw, value


def read_recording(stream: BinaryIO, *, max_events: int = 100_000) -> Recording:
    """Load and validate the complete journal before exposing immutable receptions.

    An unknown schema/dialect, bad frame, inconsistent time/count/digest, missing
    footer or any suffix after it rejects the file. At most ``max_events`` frames
    are retained. Empty journals are valid. The recorded codec version is exposed
    as metadata, not treated as a proof that the current decoder is identical.

    Replay uses ``event.received_at`` as the cache update time, then ``ended_at``
    for a final snapshot. This reproduces recorded receptions and final silence,
    not the scheduling of a live consumer whose processing times were not logged.
    """
    if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 0:
        raise RecordingError("max_events must be a nonnegative integer")
    codec = _Codec()
    raw, header = _line(stream, 1)
    if (set(header) != _HEADER_KEYS or header["kind"] != "header"
            or header["format"] != "argos.mavlink.rx"
            or type(header["version"]) is not int or header["version"] not in (1, 2, 3)
            or header["dialect"] != "ardupilotmega" or header["clock"] != "local_receive"):
        raise RecordingError("unsupported recording header, version, dialect or clock")
    codec_version = header["codec_version"]
    if not isinstance(codec_version, str) or not 1 <= len(codec_version) <= 64:
        raise RecordingError("codec_version must be a nonempty version string")
    started_at = clock = _stamp(header["started_at"], 0.)
    digest = hashlib.sha256(raw)
    events = []
    number = 1
    context = None
    if header["version"] in (2, 3):
        number += 1
        raw, entry = _line(stream, number, limit=MAX_CONTEXT_BYTES)
        if set(entry) != {"kind", "data"} or entry["kind"] != "context":
            raise RecordingError("versions 2 and 3 require one context record immediately after the header")
        context = _freeze(normalize_context(entry["data"]))
        digest.update(raw)
    while True:
        number += 1
        raw, entry = _line(stream, number)
        if entry.get("kind") == "rx":
            if set(entry) != {"kind", "received_at", "frame_hex"}:
                raise RecordingError(f"line {number}: invalid rx record fields")
            if len(events) >= max_events:
                raise RecordingError(f"journal exceeds max_events={max_events}")
            stamp = _stamp(entry["received_at"], clock)
            hexadecimal = entry["frame_hex"]
            if (not isinstance(hexadecimal, str) or not 16 <= len(hexadecimal) <= 560
                    or len(hexadecimal) % 2 or re.fullmatch("[0-9a-f]+", hexadecimal) is None):
                raise RecordingError(f"line {number}: frame_hex must encode 8..280 bytes")
            try:
                event = codec.decode(bytes.fromhex(hexadecimal), stamp)
            except RecordingError as exc:
                raise RecordingError(f"line {number}: {exc}") from exc
            events.append(event)
            clock = stamp
            digest.update(raw)
        elif entry.get("kind") == "end":
            end_keys = {"kind", "ended_at", "events"}
            if header["version"] == 3:
                end_keys |= {"reason", "detail"}
            if set(entry) != end_keys:
                raise RecordingError(f"line {number}: invalid end record fields")
            reason, detail = (_completion(entry["reason"], entry["detail"])
                              if header["version"] == 3 else (None, ""))
            ended_at = _stamp(entry["ended_at"], clock)
            if type(entry["events"]) is not int or entry["events"] != len(events):
                raise RecordingError("end record event count disagrees with the journal")
            digest.update(raw)
            _, footer = _line(stream, number + 1)
            if set(footer) != {"kind", "sha256"} or footer["kind"] != "checksum":
                raise RecordingError("journal must end with a checksum footer")
            if footer["sha256"] != digest.hexdigest():
                raise RecordingError("journal SHA-256 does not match its completion footer")
            if stream.read(1) != b"":
                raise RecordingError("journal contains bytes after its completion footer")
            return Recording(started_at, ended_at, codec_version, tuple(events), context, reason, detail)
        else:
            raise RecordingError(f"line {number}: unknown record kind")
