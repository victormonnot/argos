"""Shared serialization of admitted live and historical telemetry samples."""
from collections.abc import Mapping
import math

from argos.backends.mavlink.health import interpret_mode


def safe_text(value):
    """Represent OS error text even when a path contains undecodable bytes."""
    return str(value).encode("utf-8", errors="backslashreplace").decode("utf-8")


def reception_view(view, limit):
    return {
        "state": view.state.value, "rx_age_s": view.rx_age, "age_limit_s": limit,
        "received_at": view.message.received_at if view.message else None,
        "fields": dict(view.message.fields) if view.message else None,
        "boot_progress": view.boot_progress.value if view.boot_progress else None,
    }


def battery_view(view, limit):
    return {
        "state": view.state.value, "rx_age_s": view.rx_age, "age_limit_s": limit,
        "received_at": view.message.received_at if view.message else None,
        "fields": dict(view.message.fields) if view.message else None,
        "voltage_v": view.voltage_v, "current_a": view.current_a,
        "remaining_percent": view.remaining_percent,
    }


def mode_view(heartbeat):
    mode = (interpret_mode(heartbeat["fields"]) if heartbeat["fields"] is not None else
            {"label": "Not received", "custom_mode": None, "known": False,
             "base_mode": None, "autopilot": None, "vehicle_type": None, "mapping": None})
    mode.update({key: heartbeat[key] for key in ("state", "rx_age_s", "age_limit_s")})
    return mode


def json_fields(value):
    """Keep decoded payloads inspectable even when telemetry rejects them.

    JSON cannot carry IEEE non-finite floats, and browsers round large 64-bit
    integers. Explicit text preserves those payloads; wire bytes remain exact.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, Mapping):
        return {key: json_fields(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, bytes)):
        return [json_fields(item) for item in value]
    return value
