"""Passive inspection of three standard telemetry streams from one component.

The cache consumes decoded ``Received`` events AFTER the link has counted the
complete stream. It owns no transport and emits nothing. Source selection is an
exact (system, component) pair; a heartbeat cannot silently select a new peer.

Each view describes the last VALID reception of its own message type. ``rx_age``
is time since local reception, not the age of the measurement: packets may have
waited in a queue before decoding. ``time_boot_ms`` stays in the sender's clock.
Its progress is reported per message type, without inferring a clock offset or
mistaking a decrease for proof of a reboot. Repeats and decreases remain visible
even when the reception is recent. This is a diagnostic cache, not a readiness
test, state estimator, or input certified for flight control.

LOCAL_POSITION_NED keeps its original axes and origin. The message establishes
neither an origin at takeoff nor a height above terrain. Converting its axes to
ENU would not establish the takeoff origin required by ``core.SelfState``.

Every call takes an explicit local run time. Successful updates and snapshots
advance the admission clock. Rejected or ignored events only affect diagnostics;
they cannot replace a sample or prevent a subsequent valid update. A delayed
consumer may pass an older received_at with a current now, but cannot replace a
sample with an earlier reception of the same message type. Equal dates are valid.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .link import Received, _time


class ReceptionState(Enum):
    ABSENT = "absent"
    RECENT = "recent"       # last valid local reception is within its age limit
    STALE = "stale"


class BootProgress(Enum):
    FIRST = "first"
    ADVANCED = "advanced"
    REPEATED = "repeated"
    DECREASED = "decreased"  # reorder, restart and uint32 wrap are not distinguished


class UpdateStatus(Enum):
    ACCEPTED = "accepted"
    IGNORED_SOURCE = "ignored_source"
    IGNORED_TYPE = "ignored_type"
    REJECTED = "rejected"


@dataclass(frozen=True)
class TelemetryUpdate:
    status: UpdateStatus
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.status is UpdateStatus.ACCEPTED


@dataclass(frozen=True)
class TelemetryLimits:
    """Local reception age limits in seconds, chosen by the diagnostic caller.

    Equality is RECENT; a strictly greater age is STALE. These are display/analysis
    thresholds, not a declaration that an aircraft can be controlled safely.
    """

    heartbeat: float
    attitude: float
    local_position_ned: float

    def __post_init__(self) -> None:
        for name in ("heartbeat", "attitude", "local_position_ned"):
            value = _time(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} age limit must be positive")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class ReceptionView:
    message: Received | None
    rx_age: float | None
    state: ReceptionState
    boot_progress: BootProgress | None


@dataclass(frozen=True)
class TelemetrySnapshot:
    at: float                # when inspected, never substituted for receipt times
    system: int
    component: int
    heartbeat: ReceptionView
    attitude: ReceptionView
    local_position_ned: ReceptionView
    accepted: int            # accepted receptions, not distinct measurements
    ignored_source: int
    ignored_type: int
    rejected: int
    last_rejection: str       # historical diagnostic; a valid update does not erase it


# These fields are the standard wire definitions, without derived vehicle state.
# An integer denotes the unsigned field width; None denotes a finite float.
_SCHEMAS = {
    0: ("HEARTBEAT", {
        "type": 8, "autopilot": 8, "base_mode": 8, "custom_mode": 32,
        "system_status": 8, "mavlink_version": 8,
    }),
    30: ("ATTITUDE", {
        "time_boot_ms": 32, "roll": None, "pitch": None, "yaw": None,
        "rollspeed": None, "pitchspeed": None, "yawspeed": None,
    }),
    32: ("LOCAL_POSITION_NED", {
        "time_boot_ms": 32, "x": None, "y": None, "z": None,
        "vx": None, "vy": None, "vz": None,
    }),
}
_NAMES = frozenset(name for name, _ in _SCHEMAS.values())


def _uint(value: object, bits: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**bits:
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")
    return value


class TelemetryCache:
    """Bounded, single-owner cache, independent of any I/O or optional codec.

    Use ``for event in link.poll(now): cache.update(event, now)`` and then
    ``cache.snapshot(now)``. Link status remains in ``link.report(now)``; retained
    samples do not imply that their transport is still open. This cache does not
    recheck frame CRCs or authenticate senders: ``MavlinkLink`` is its decoder.
    """

    def __init__(self, *, system: int, component: int,
                 limits: TelemetryLimits, started_at: float = 0.) -> None:
        for name, value in (("system", system), ("component", component)):
            _uint(value, 8, name)
            if value == 0:
                raise ValueError(f"{name} must identify one source in 1..255")
        if not isinstance(limits, TelemetryLimits):
            raise TypeError("limits must be TelemetryLimits")
        started_at = _time(started_at)
        if started_at < 0:
            raise ValueError("started_at must be nonnegative run time")
        self._system, self._component = system, component
        self._limits = limits
        self._started_at = self._clock = started_at
        self._samples: dict[int, Received] = {}
        self._progress: dict[int, BootProgress | None] = {}
        self._accepted = self._ignored_source = self._ignored_type = self._rejected = 0
        self._last_rejection = ""

    def _checked_time(self, now: float) -> float:
        now = _time(now)
        if now < self._clock:
            raise ValueError("call time predates the last accepted call")
        return now

    def _reject(self, detail: str) -> TelemetryUpdate:
        self._rejected += 1
        self._last_rejection = detail
        return TelemetryUpdate(UpdateStatus.REJECTED, detail)

    def update(self, event: Received, now: float) -> TelemetryUpdate:
        """Inspect one decoded arrival; preserve receipt time across caller delays.

        Invalid call times raise before any mutation. Invalid event data returns
        REJECTED and increments its diagnostic, preserving previous valid data and
        the admission clock. Ignored sources/types also leave that clock alone.
        """
        now = self._checked_time(now)
        if not isinstance(event, Received):
            return self._reject("event must be a decoded Received message")
        try:
            _uint(event.system, 8, "system")
            _uint(event.component, 8, "component")
        except ValueError as exc:
            return self._reject(str(exc))
        if (event.system, event.component) != (self._system, self._component):
            self._ignored_source += 1
            return TelemetryUpdate(UpdateStatus.IGNORED_SOURCE)
        try:
            _uint(event.message_id, 24, "message_id")
            if not isinstance(event.type_name, str):
                raise ValueError("type_name must be a string")
            schema = _SCHEMAS.get(event.message_id)
            if schema is None:
                if event.type_name in _NAMES:
                    raise ValueError("message id and type name disagree")
                self._ignored_type += 1
                return TelemetryUpdate(UpdateStatus.IGNORED_TYPE)
            name, fields = schema
            if event.type_name != name:
                raise ValueError("message id and type name disagree")
            _uint(event.sequence, 8, "sequence")
            received_at = _time(event.received_at)
            if not self._started_at <= received_at <= now:
                raise ValueError("receipt must be between started_at and now")
            previous = self._samples.get(event.message_id)
            if previous is not None and received_at < previous.received_at:
                raise ValueError("receipt predates the last valid reception of this type")
            if not isinstance(event.fields, Mapping):
                raise ValueError("message fields must be a mapping")
            if "mavpackettype" in event.fields and event.fields["mavpackettype"] != name:
                raise ValueError("payload type name disagrees with the message header")
            payload: dict[str, object] = {"mavpackettype": name}
            for key, bits in fields.items():
                if key not in event.fields:
                    raise ValueError(f"missing field: {key}")
                value = event.fields[key]
                if bits is None:
                    try:
                        payload[key] = _time(value)
                    except ValueError as exc:
                        raise ValueError(f"{key} must be a finite real number") from exc
                else:
                    payload[key] = _uint(value, bits, key)
            if not isinstance(event.frame, bytes):
                raise ValueError("decoded frame must be bytes")
        except ValueError as exc:
            return self._reject(str(exc))

        progress = None
        if "time_boot_ms" in payload:
            progress = BootProgress.FIRST
            if previous is not None:
                boot, old = payload["time_boot_ms"], previous.fields["time_boot_ms"]
                progress = (BootProgress.ADVANCED if boot > old else
                            BootProgress.REPEATED if boot == old else BootProgress.DECREASED)
        # All retained payload values are detached immutable scalars. Keeping the
        # original frozen dataclass alone would not detach a caller-owned dict.
        sample = Received(received_at, event.system, event.component, event.sequence,
                          event.message_id, name, MappingProxyType(payload), event.frame)
        self._samples[event.message_id] = sample
        self._progress[event.message_id] = progress
        self._accepted += 1
        self._clock = now
        return TelemetryUpdate(UpdateStatus.ACCEPTED)

    def snapshot(self, now: float) -> TelemetrySnapshot:
        """Inspect the last valid receptions, including stale retained values.

        Reading does not refresh any reception. Data that failed validation is
        never substituted for the last valid sample; consult rejection diagnostics
        alongside the views. RECENT describes reception only, even after a boot
        counter repeat or decrease, and never guarantees measurement freshness.
        """
        now = self._checked_time(now)

        def view(message_id: int, limit: float) -> ReceptionView:
            message = self._samples.get(message_id)
            if message is None:
                return ReceptionView(None, None, ReceptionState.ABSENT, None)
            age = now - message.received_at
            state = ReceptionState.RECENT if age <= limit else ReceptionState.STALE
            return ReceptionView(message, age, state, self._progress[message_id])

        result = TelemetrySnapshot(
            now, self._system, self._component,
            view(0, self._limits.heartbeat), view(30, self._limits.attitude),
            view(32, self._limits.local_position_ned),
            self._accepted, self._ignored_source, self._ignored_type,
            self._rejected, self._last_rejection,
        )
        self._clock = now
        return result
