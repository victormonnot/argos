"""Validated capture provenance for the passive console.

Wall-clock UTC identifies when capture began. Age limits describe the console's
local receipt policy; they are not camera exposure or vehicle measurement age.
Unknown context schemas can remain opaque to this consumer.
"""
from collections.abc import Mapping
from datetime import datetime, timezone
from ipaddress import IPv4Address
import math
import re

from argos.backends.mavlink.recording import normalize_context
from .config import ConsoleConfig


FORMAT = "argos.console.capture"
_KEYS = {"format", "version", "captured_at_utc", "run_id", "configuration",
         "telemetry_endpoint", "age_limits_s"}
_LIMITS = {"video", "heartbeat", "battery", "attitude", "local_position_ned"}


def _utc(value):
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value) is None:
            raise ValueError("capture timestamp must be an ISO 8601 UTC timestamp")
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("capture timestamp is not a valid UTC date") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _endpoint(value, config):
    if config.mavlink_bind is None:
        if value != config.telemetry_endpoint:
            raise ValueError("capture endpoint disagrees with its source configuration")
        return value
    if not isinstance(value, str):
        raise ValueError("capture UDP endpoint must be a string")
    match = re.fullmatch(r"UDP ([0-9.]+):([0-9]+) ← ([0-9.]+):([0-9]+)", value)
    if match is None:
        raise ValueError("capture UDP endpoint is invalid")
    local = str(IPv4Address(match[1])), int(match[2])
    peer = str(IPv4Address(match[3])), int(match[4])
    if (not 0 <= local[1] <= 65535 or peer != config.mavlink_peer
            or config.mavlink_bind[0] not in ("0.0.0.0", local[0])
            or config.mavlink_bind[1] not in (0, local[1])):
        raise ValueError("capture UDP endpoint disagrees with its source configuration")
    return value


def parse_capture_context(value) -> dict | None:
    """Return validated JSON data, or None for an absent/unknown schema.

    A known ARGOS schema with invalid values raises ValueError. Consumers must
    not present its labels or age thresholds as established capture metadata.
    The returned object is a detached mutable copy of the immutable recording.
    """
    if not isinstance(value, Mapping) or value.get("format") != FORMAT:
        return None
    if type(value.get("version")) is not int:
        raise ValueError("capture context version must be an integer")
    if value["version"] != 1:
        return None
    value = normalize_context(value)
    if set(value) != _KEYS:
        raise ValueError("capture context must contain exactly the documented fields")
    if not isinstance(value["run_id"], str) or re.fullmatch("[0-9a-f]{32}", value["run_id"]) is None:
        raise ValueError("capture run_id must be a lowercase UUID hexadecimal identifier")
    timestamp = _utc(value["captured_at_utc"])
    try:
        config = ConsoleConfig().with_sources(value["configuration"])
    except (TypeError, ValueError, OverflowError) as exc:
        # Stored JSON is untrusted data. Configuration validators may use
        # enum/set lookups whose wrong input types raise TypeError; callers
        # need one stable validation exception for every malformed context.
        raise ValueError(f"capture source configuration is invalid: {exc}") from exc
    limits = value["age_limits_s"]
    if not isinstance(limits, dict) or set(limits) != _LIMITS:
        raise ValueError("capture age limits must contain exactly the documented sources")
    for name, limit in limits.items():
        try:
            valid = type(limit) in (int, float) and math.isfinite(limit) and limit > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"capture age limit {name} must be finite and positive")
    return {"format": FORMAT, "version": 1, "captured_at_utc": timestamp,
            "run_id": value["run_id"], "configuration": config.public(),
            "telemetry_endpoint": _endpoint(value["telemetry_endpoint"], config),
            "age_limits_s": {name: float(limits[name]) for name in sorted(_LIMITS)}}


def capture_context(config: ConsoleConfig, run_id: str, telemetry_endpoint: str | None,
                    *, captured_at_utc=None) -> dict:
    """Snapshot the original source selection and independent receipt limits."""
    value = {"format": FORMAT, "version": 1,
             "captured_at_utc": _utc(datetime.now(timezone.utc) if captured_at_utc is None else captured_at_utc),
             "run_id": run_id, "configuration": config.public(),
             "telemetry_endpoint": telemetry_endpoint,
             "age_limits_s": {"video": config.video_age, "heartbeat": config.limits.heartbeat,
                              "battery": config.battery_age, "attitude": config.limits.attitude,
                              "local_position_ned": config.limits.local_position_ned}}
    return parse_capture_context(value)
