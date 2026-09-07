"""Declared HEARTBEAT status and bounded, passive STATUSTEXT history.

These are reports from the selected component, not a health assessment. History
is local to one console session and keeps its connection provenance on reconnect.
STATUSTEXT absence or silence does not mean that there are no vehicle problems.

Protocol: https://mavlink.io/en/messages/common.html#STATUSTEXT and
https://mavlink.io/en/messages/minimal.html#MAV_STATE . Nonzero IDs are assembled
only in order, from chunk zero, on one connection. A NUL ends a long message;
ID zero is immediately complete, even when all 50 text bytes are occupied.
Gaps/conflicts/expiry/capacity produce explicitly incomplete fragments, never a
sentence made by silently joining text across a missing chunk. Limits and timeout
are local retention policies, not protocol deadlines or evidence of RF loss.

Received.frame has already been CRC-validated by MavlinkLink. Its text bytes are
used because pymavlink 2.4.49's decoded text uses lossy ASCII replacement despite
the UTF-8 wire definition. Decode after assembly so a code point can cross chunks.
This helper owns no transport and never requests messages or emits commands.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field

from argos.backends.mavlink import Received
from argos.backends.mavlink.link import _time
from argos.backends.mavlink.telemetry import _uint


_STATES = (
    ("UNINIT", "Non initialisé"), ("BOOT", "Démarrage"),
    ("CALIBRATING", "Calibration"), ("STANDBY", "En attente"),
    ("ACTIVE", "Actif"), ("CRITICAL", "Critique"),
    ("EMERGENCY", "Urgence"), ("POWEROFF", "Arrêt en cours"),
    ("FLIGHT_TERMINATION", "Terminaison de vol"),
)
_SEVERITIES = (
    ("EMERGENCY", "Urgence"), ("ALERT", "Alerte"),
    ("CRITICAL", "Critique"), ("ERROR", "Erreur"),
    ("WARNING", "Avertissement"), ("NOTICE", "Notification"),
    ("INFO", "Information"), ("DEBUG", "Débogage"),
)


def system_status_view(heartbeat: Mapping) -> dict:
    """Label the status byte of an already admitted HEARTBEAT reception view."""
    fields = heartbeat["fields"]
    code = None if fields is None else _uint(fields["system_status"], 8, "system_status")
    known = code is not None and code < len(_STATES)
    return {
        "state": "unknown" if fields is None else heartbeat["state"],
        "system_status": code,
        "name": "MAV_STATE_" + _STATES[code][0] if known else None,
        "label": _STATES[code][1] if known else "Non reçu" if code is None else f"Inconnu ({code})",
        "known": known,
        "received_at": heartbeat["received_at"],
        "rx_age_s": heartbeat["rx_age_s"], "age_limit_s": heartbeat["age_limit_s"],
    }


def _bounded_integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def _connection(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("connection_id must be a nonempty bounded string")
    return value


def _chunk(event: Received) -> tuple[int, int, int, bytes, bool]:
    """Read the fixed STATUSTEXT payload; validate provenance, not a second CRC.

    MAVLink 2 omits trailing zero payload bytes. Restore those before testing for
    the terminal NUL and reading extension fields. MAVLink 1 has no extensions.
    """
    frame = event.frame
    if not isinstance(frame, bytes) or len(frame) < 8:
        raise ValueError("STATUSTEXT requires its original MAVLink frame")
    size = frame[1]
    if frame[0] == 0xFE:
        offset, suffix = 6, 2
        if size != 51 or frame[5] != 253:
            raise ValueError("invalid MAVLink 1 STATUSTEXT payload")
        sequence, system, component = frame[2:5]
    elif frame[0] == 0xFD and len(frame) >= 12:
        offset, suffix = 10, 2 + (13 if frame[2] & 1 else 0)
        if frame[2] & ~1 or not 1 <= size <= 54 or int.from_bytes(frame[7:10], "little") != 253:
            raise ValueError("invalid MAVLink 2 STATUSTEXT payload")
        sequence, system, component = frame[4:7]
    else:
        raise ValueError("invalid STATUSTEXT frame version")
    if len(frame) != offset + size + suffix:
        raise ValueError("STATUSTEXT frame length disagrees with its header")
    if (sequence, system, component) != (event.sequence, event.system, event.component):
        raise ValueError("STATUSTEXT provenance disagrees with its frame")
    payload = frame[offset:offset + size].ljust(54, b"\0")
    severity, status_id, sequence = payload[0], int.from_bytes(payload[51:53], "little"), payload[53]
    fields = event.fields
    if not isinstance(fields, Mapping) or fields.get("mavpackettype", "STATUSTEXT") != "STATUSTEXT":
        raise ValueError("invalid STATUSTEXT fields")
    for name, value, bits in (("severity", severity, 8), ("id", status_id, 16), ("chunk_seq", sequence, 8)):
        declared = fields.get(name, 0) if name != "severity" else fields.get(name)
        if _uint(declared, bits, name) != value:
            raise ValueError(f"STATUSTEXT {name} disagrees with its frame")
    if not isinstance(fields.get("text"), (str, bytes)):
        raise ValueError("STATUSTEXT text must be decoded text or bytes")
    if status_id == 0 and sequence != 0:
        raise ValueError("a standalone STATUSTEXT must have chunk_seq zero")
    raw = payload[1:51]
    return severity, status_id, sequence, raw.split(b"\0", 1)[0], b"\0" in raw


@dataclass
class _Text:
    id: int
    connection_id: str
    status_id: int
    severity: int
    first_received_at: float
    received_at: float
    parts: list[bytes] = field(default_factory=list)
    wire_sequences: list[int] = field(default_factory=list)
    complete: bool = False
    reason: str | None = "assembling"


class StatusTexts:
    """Single owner; selected source; bounded recent history and assembly memory.

    append() accepts decoded receipts from link.poll(), returns whether the input
    was admitted, and counts malformed input without replacing valid history.
    snapshot() expires assemblies even during silence. A duplicate means the same
    chunk bytes AND transport sequence in a pending assembly: it never refreshes
    that assembly's age. A reused ID starting at zero otherwise starts a new text.
    Source IDs are declared identities; this receiver does not authenticate them.
    """

    def __init__(self, *, system: int, component: int, connection_id: str,
                 started_at: float = 0., max_entries: int = 60, max_pending: int = 8,
                 max_chunks: int = 32, chunk_timeout_s: float = 2.):
        self.system = _bounded_integer(system, 1, 255, "system")
        self.component = _bounded_integer(component, 1, 255, "component")
        self.connection_id = _connection(connection_id)
        self.max_entries = _bounded_integer(max_entries, 1, 200, "max_entries")
        self.max_pending = _bounded_integer(max_pending, 1, min(32, max_entries), "max_pending")
        self.max_chunks = _bounded_integer(max_chunks, 1, 256, "max_chunks")
        self.chunk_timeout_s = _time(chunk_timeout_s)
        if not 0 < self.chunk_timeout_s <= 60:
            raise ValueError("chunk_timeout_s must be positive and at most 60 seconds")
        self._clock = self._generation_start = _time(started_at)
        if self._clock < 0:
            raise ValueError("started_at must be nonnegative")
        self._last_received = self._generation_start
        self._entries: OrderedDict[int, _Text] = OrderedDict()
        self._pending: OrderedDict[int, _Text] = OrderedDict()
        self._next_id = 0
        self.evicted_entries = self.rejected = self.duplicates = 0
        self.last_rejection = ""

    def _now(self, now):
        now = _time(now)
        if now < self._clock:
            raise ValueError("status history clock moved backwards")
        return now

    def _finish(self, item, reason=None):
        item.complete, item.reason = reason is None, reason
        if self._pending.get(item.status_id) is item:
            del self._pending[item.status_id]

    def _expire(self, now):
        for item in tuple(self._pending.values()):
            if now - item.received_at >= self.chunk_timeout_s:
                self._finish(item, "timeout")

    def _new(self, severity, status_id, received_at, data, sequence):
        if len(self._entries) >= self.max_entries:
            _, oldest = self._entries.popitem(last=False)
            self._finish(oldest, "limit")
            self.evicted_entries += 1
        self._next_id += 1
        item = _Text(self._next_id, self.connection_id, status_id, severity,
                     received_at, received_at, [data], [sequence])
        self._entries[item.id] = item
        return item

    def append(self, event: Received, *, now: float) -> bool:
        now = self._now(now)
        try:
            if not isinstance(event, Received):
                raise ValueError("event must be a decoded Received message")
            _uint(event.system, 8, "system")
            _uint(event.component, 8, "component")
            if (event.system, event.component) != (self.system, self.component):
                return False
            _uint(event.message_id, 24, "message_id")
            if event.message_id != 253:
                if event.type_name == "STATUSTEXT":
                    raise ValueError("STATUSTEXT message id and name disagree")
                return False
            if event.type_name != "STATUSTEXT":
                raise ValueError("STATUSTEXT message id and name disagree")
            _uint(event.sequence, 8, "sequence")
            received_at = _time(event.received_at)
            if not self._last_received <= received_at <= now:
                raise ValueError("STATUSTEXT receipt is old, from another connection, or in the future")
            severity, status_id, chunk_seq, data, terminal = _chunk(event)
        except ValueError as exc:
            self.rejected += 1
            self.last_rejection = str(exc)
            return False

        self._clock, self._last_received = now, received_at
        self._expire(now)
        previous = self._pending.get(status_id)
        if previous is not None and chunk_seq < len(previous.parts):
            if (previous.parts[chunk_seq] == data and previous.severity == severity
                    and previous.wire_sequences[chunk_seq] == event.sequence):
                self.duplicates += 1
                return True

        if status_id == 0 or chunk_seq == 0:
            if previous is not None:
                self._finish(previous, "restarted")
            item = self._new(severity, status_id, received_at, data, event.sequence)
            if status_id == 0 or terminal:
                self._finish(item)
            elif self.max_chunks == 1:
                self._finish(item, "limit")
            else:
                if len(self._pending) >= self.max_pending:
                    self._finish(next(iter(self._pending.values())), "limit")
                self._pending[status_id] = item
        elif previous is None:
            self._finish(self._new(severity, status_id, received_at, data, event.sequence), "missing_chunk")
        elif previous.severity != severity or chunk_seq != len(previous.parts):
            reason = ("severity_changed" if previous.severity != severity else
                      "missing_chunk" if chunk_seq > len(previous.parts) else "conflicting_chunk")
            self._finish(previous, reason)
            self._finish(self._new(severity, status_id, received_at, data, event.sequence), reason)
        else:
            previous.parts.append(data)
            previous.wire_sequences.append(event.sequence)
            previous.received_at = received_at
            self._entries.move_to_end(previous.id)
            self._pending.move_to_end(status_id)
            if terminal:
                self._finish(previous)
            elif len(previous.parts) >= self.max_chunks or chunk_seq == 255:
                self._finish(previous, "limit")
        self._expire(now)
        return True

    def reconnect(self, connection_id: str, now: float) -> None:
        """End pending texts before a new receiver can supply any chunks.

        Existing historical entries keep the old connection ID and receipt dates.
        Call on MAVLink receiver replacement, including a failed reopen; a camera
        reconnect alone must not call this method. Source changes create a new owner.
        """
        now = self._now(now)
        connection_id = _connection(connection_id)
        if connection_id == self.connection_id:
            raise ValueError("a reconnect requires a new connection identifier")
        for item in tuple(self._pending.values()):
            self._finish(item, "reconnect")
        self.connection_id = connection_id
        self._clock = self._generation_start = self._last_received = now

    def snapshot(self, now: float) -> dict:
        now = self._now(now)
        self._expire(now)
        self._clock = now
        entries = []
        for item in reversed(self._entries.values()):
            raw = b"".join(item.parts)
            try:
                text = raw.decode("utf-8")
                utf8_valid = True
            except UnicodeDecodeError:
                text, utf8_valid = raw.decode("utf-8", errors="replace"), False
            known = item.severity < len(_SEVERITIES)
            entries.append({
                "id": item.id, "system": self.system, "component": self.component,
                "connection_id": item.connection_id, "status_id": item.status_id,
                "severity": item.severity,
                "severity_name": "MAV_SEVERITY_" + _SEVERITIES[item.severity][0] if known else None,
                "severity_label": _SEVERITIES[item.severity][1] if known else f"Inconnu ({item.severity})",
                "text": text, "complete": item.complete, "reason": item.reason,
                "utf8_valid": utf8_valid, "first_received_at": item.first_received_at,
                "received_at": item.received_at, "rx_age_s": now - item.received_at,
                "chunks": len(item.parts),
            })
        return {
            "system": self.system, "component": self.component,
            "connection_id": self.connection_id, "entries": entries,
            "max_entries": self.max_entries, "evicted_entries": self.evicted_entries,
            "pending": len(self._pending), "max_pending": self.max_pending,
            "max_chunks": self.max_chunks, "chunk_timeout_s": self.chunk_timeout_s,
            "rejected": self.rejected, "last_rejection": self.last_rejection,
            "duplicates": self.duplicates,
        }
