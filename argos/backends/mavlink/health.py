"""Passive battery reception and flight-mode labels for display.

SYS_STATUS describes an aggregate battery report, not an identified battery pack.
Its voltage/current/remaining fields may each be unavailable independently. Only
these three fields are admitted and retained here; sensor-health and error fields
are outside this view. No readiness or physical measurement freshness is inferred.

Sources: https://mavlink.io/en/messages/common.html#SYS_STATUS and
https://ardupilot.org/dev/docs/mavlink-get-set-flightmode.html .
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .link import Received, _time
from .telemetry import ReceptionState, TelemetryUpdate, UpdateStatus, _uint


@dataclass(frozen=True)
class BatteryView:
    message: Received | None
    rx_age: float | None
    state: ReceptionState
    voltage_v: float | None
    current_a: float | None
    remaining_percent: int | None


@dataclass(frozen=True)
class HealthSnapshot:
    at: float
    system: int
    component: int
    battery: BatteryView
    accepted: int
    ignored_source: int
    ignored_type: int
    rejected: int
    last_rejection: str


def _integer(value: object, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


class HealthCache:
    """Single-source SYS_STATUS view with an explicit local reception age limit.

    Uses the same admission rules as TelemetryCache: successful calls advance the
    local clock; ignored/rejected events preserve the clock and last valid sample.
    The input is an already decoded Received event, without further CRC checking.
    A stale sample remains inspectable. Each unavailable wire sentinel becomes
    None, while measured zero and other signed current values remain intact.
    """

    def __init__(self, *, system: int, component: int, age_limit: float = 2.,
                 started_at: float = 0.) -> None:
        self._system = _integer(system, 1, 255, "system")
        self._component = _integer(component, 1, 255, "component")
        self._age_limit = _time(age_limit)
        if self._age_limit <= 0:
            raise ValueError("age_limit must be positive")
        self._started_at = self._clock = _time(started_at)
        if self._started_at < 0:
            raise ValueError("started_at must be nonnegative run time")
        self._sample: Received | None = None
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
            if event.message_id != 1:
                if event.type_name == "SYS_STATUS":
                    raise ValueError("message id and type name disagree")
                self._ignored_type += 1
                return TelemetryUpdate(UpdateStatus.IGNORED_TYPE)
            if event.type_name != "SYS_STATUS":
                raise ValueError("message id and type name disagree")
            _uint(event.sequence, 8, "sequence")
            received_at = _time(event.received_at)
            if not self._started_at <= received_at <= now:
                raise ValueError("receipt must be between started_at and now")
            if self._sample is not None and received_at < self._sample.received_at:
                raise ValueError("receipt predates the last valid reception of this type")
            if not isinstance(event.fields, Mapping):
                raise ValueError("message fields must be a mapping")
            if event.fields.get("mavpackettype", "SYS_STATUS") != "SYS_STATUS":
                raise ValueError("payload type name disagrees with the message header")
            payload: dict[str, object] = {"mavpackettype": "SYS_STATUS"}
            for name, low, high in (("voltage_battery", 0, 65535),
                                    ("current_battery", -32768, 32767),
                                    ("battery_remaining", -1, 100)):
                if name not in event.fields:
                    raise ValueError(f"missing field: {name}")
                payload[name] = _integer(event.fields[name], low, high, name)
            if not isinstance(event.frame, bytes):
                raise ValueError("decoded frame must be bytes")
        except ValueError as exc:
            return self._reject(str(exc))

        self._sample = Received(received_at, event.system, event.component,
                                event.sequence, 1, "SYS_STATUS",
                                MappingProxyType(payload), event.frame)
        self._accepted += 1
        self._clock = now
        return TelemetryUpdate(UpdateStatus.ACCEPTED)

    def snapshot(self, now: float) -> HealthSnapshot:
        now = self._checked_time(now)
        sample = self._sample
        if sample is None:
            battery = BatteryView(None, None, ReceptionState.ABSENT, None, None, None)
        else:
            fields = sample.fields
            age = now - sample.received_at
            state = ReceptionState.RECENT if age <= self._age_limit else ReceptionState.STALE
            battery = BatteryView(
                sample, age, state,
                None if fields["voltage_battery"] == 65535 else fields["voltage_battery"] / 1000.,
                None if fields["current_battery"] == -1 else fields["current_battery"] / 100.,
                None if fields["battery_remaining"] == -1 else fields["battery_remaining"],
            )
        result = HealthSnapshot(now, self._system, self._component, battery,
                                self._accepted, self._ignored_source,
                                self._ignored_type, self._rejected, self._last_rejection)
        self._clock = now
        return result


def interpret_mode(heartbeat_fields: Mapping[str, object]) -> dict[str, object]:
    """Label an advertised ArduCopter mode without interpreting another family.

    Input must contain valid decoded heartbeat mode/type fields. Invalid fields
    raise ValueError; unsupported firmware/types/IDs keep their raw identifiers
    and receive an explicit unknown label. This is a display mapping using the
    installed pymavlink table, not firmware identification or vehicle readiness.
    No optional library is imported until an eligible Copter heartbeat is used.
    """
    if not isinstance(heartbeat_fields, Mapping):
        raise ValueError("heartbeat fields must be a mapping")
    values: dict[str, int] = {}
    for name, bits in (("type", 8), ("autopilot", 8), ("base_mode", 8), ("custom_mode", 32)):
        if name not in heartbeat_fields:
            raise ValueError(f"missing field: {name}")
        values[name] = _uint(heartbeat_fields[name], bits, name)
    custom = values["custom_mode"]
    result: dict[str, object] = {
        "label": f"Inconnu ({custom})", "custom_mode": custom,
        "base_mode": values["base_mode"], "autopilot": values["autopilot"],
        "vehicle_type": values["type"], "known": False, "mapping": None,
    }
    # Standard MAVLink enum values: ARDUPILOTMEGA=3, CUSTOM_MODE_ENABLED=1,
    # QUADROTOR/COAXIAL/HELICOPTER/HEXAROTOR/OCTOROTOR/TRICOPTER/DODECAROTOR.
    if (values["autopilot"] == 3 and values["base_mode"] & 1
            and values["type"] in (2, 3, 4, 13, 14, 15, 29)):
        from pymavlink.mavutil import mode_mapping_acm

        result["mapping"] = "arducopter"
        label = mode_mapping_acm.get(custom)
        if label is not None:
            result["label"], result["known"] = label, True
    return result
