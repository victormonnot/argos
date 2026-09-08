"""Explicit source identity, independent of the web presentation."""
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re

from argos.backends.mavlink import SequenceScope, TelemetryCache, TelemetryLimits
from argos.backends.mavlink.link import _time
from argos.backends.mavlink.transport import _address


def default_recordings_dir():
    """Stable per-user storage, independent of the launch working directory."""
    configured = os.environ.get("XDG_DATA_HOME", "")
    root = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local" / "share"
    return root / "argos" / "recordings"


@dataclass(frozen=True)
class ConsoleConfig:
    sim_control: bool = False
    sim_framing: bool = False
    vision_model: Path | None = None
    video_source: str = "none"
    video_endpoint: str | None = None
    environment: str = "unconfigured"
    gazebo_python_path: Path | None = None
    video_age: float = 1.
    mavlink_bind: tuple[str, int] | None = None
    mavlink_peer: tuple[str, int] | None = None
    mavlink_tcp: tuple[str, int] | None = None
    mavlink_device: str | None = None
    baudrate: int = 115200
    sequence_scope: SequenceScope | None = None
    system: int = 1
    component: int = 1
    battery_age: float = 2.
    recordings_dir: Path = field(default_factory=default_recordings_dir)
    limits: TelemetryLimits = field(default_factory=lambda: TelemetryLimits(1., .2, .4))

    def __post_init__(self):
        if not isinstance(self.sim_framing, bool):
            raise ValueError("sim_framing must be a boolean")
        if self.sim_framing and (not self.sim_control or self.vision_model is None
                or self.video_source != "gazebo"
                or self.video_endpoint != "/world/iris_runway/model/iris_with_gimbal/model/gimbal/link/pitch_link/sensor/camera/image"):
            raise ValueError("framing requires simulation control, a vision model and the validated person-scene camera")
        if self.vision_model is not None:
            object.__setattr__(self, "vision_model", Path(self.vision_model).expanduser().resolve())
        if not isinstance(self.sim_control, bool):
            raise ValueError("sim_control must be a boolean")
        if self.video_source not in ("none", "gazebo", "device"):
            raise ValueError("video source must be none, gazebo or device")
        if self.environment not in ("unconfigured", "simulation", "real"):
            raise ValueError("choose simulation or real for configured sources")
        if self.video_source == "none" and self.video_endpoint is not None:
            raise ValueError("video endpoint needs a camera source")
        if self.video_source == "gazebo":
            if (not isinstance(self.video_endpoint, str)
                    or len(self.video_endpoint) > 1024
                    or not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", self.video_endpoint)):
                raise ValueError("Gazebo needs an explicit absolute image topic")
            if self.environment != "simulation":
                raise ValueError("Gazebo camera belongs to the simulation environment")
        if self.video_source == "device":
            if not isinstance(self.video_endpoint, str) or not re.fullmatch(r"/dev/video[0-9]+", self.video_endpoint):
                raise ValueError("real camera must be a Linux /dev/videoN device")
            if self.environment != "real":
                raise ValueError("a physical camera belongs to the real environment")
        video_age = _time(self.video_age)
        if video_age <= 0:
            raise ValueError("video age limit must be positive")
        object.__setattr__(self, "video_age", video_age)
        battery_age = _time(self.battery_age)
        if battery_age <= 0:
            raise ValueError("battery age limit must be positive")
        object.__setattr__(self, "battery_age", battery_age)
        if (self.mavlink_bind is None) != (self.mavlink_peer is None):
            raise ValueError("MAVLink UDP requires both bind and peer")
        if self.mavlink_bind is not None:
            object.__setattr__(self, "mavlink_bind", _address(self.mavlink_bind, ephemeral=True))
            object.__setattr__(self, "mavlink_peer", _address(self.mavlink_peer, ephemeral=False))
        if self.mavlink_tcp is not None:
            if self.mavlink_bind is not None or self.mavlink_device is not None:
                raise ValueError("choose only one MAVLink transport")
            object.__setattr__(self, "mavlink_tcp", _address(self.mavlink_tcp, ephemeral=False))
        if self.mavlink_device is not None:
            if self.mavlink_bind is not None:
                raise ValueError("choose either MAVLink UDP or serial")
            if not isinstance(self.mavlink_device, str) or not self.mavlink_device:
                raise ValueError("MAVLink serial device must be a nonempty path")
        if self.sim_control and (self.environment != "simulation"
                                 or self.mavlink_tcp is None
                                 or self.mavlink_tcp[0] != "127.0.0.1"):
            raise ValueError("web control requires an explicit simulation and loopback SITL TCP connection")
        if isinstance(self.baudrate, bool) or not isinstance(self.baudrate, int) or self.baudrate <= 0:
            raise ValueError("baudrate must be a positive integer")
        if self.has_telemetry:
            if not isinstance(self.sequence_scope, SequenceScope):
                raise ValueError("MAVLink requires an explicit sequence scope")
            if self.environment == "unconfigured":
                raise ValueError("declare simulation or real for the MAVLink source")
        elif self.sequence_scope is not None:
            raise ValueError("sequence scope requires a MAVLink source")
        # Reuse admission configuration without importing the MAVLink codec.
        TelemetryCache(system=self.system, component=self.component, limits=self.limits)

    @property
    def has_telemetry(self):
        return self.mavlink_bind is not None or self.mavlink_device is not None or self.mavlink_tcp is not None

    @property
    def telemetry_endpoint(self):
        if self.mavlink_tcp is not None:
            return f"TCP {self.mavlink_tcp[0]}:{self.mavlink_tcp[1]}"
        if self.mavlink_bind is not None:
            local = f"{self.mavlink_bind[0]}:{self.mavlink_bind[1]}"
            peer = f"{self.mavlink_peer[0]}:{self.mavlink_peer[1]}"
            return f"UDP {local} ← {peer}"
        if self.mavlink_device is not None:
            return f"{self.mavlink_device} · {self.baudrate} bauds"
        return None

    def public(self):
        def address(value):
            return f"{value[0]}:{value[1]}" if value is not None else None
        kind = ("tcp" if self.mavlink_tcp else "udp" if self.mavlink_bind else
                "serial" if self.mavlink_device else "none")
        return {"environment": self.environment, "video_source": self.video_source,
                "video_endpoint": self.video_endpoint, "mavlink_transport": kind,
                "mavlink_bind": address(self.mavlink_bind), "mavlink_peer": address(self.mavlink_peer),
                "mavlink_tcp": address(self.mavlink_tcp), "mavlink_device": self.mavlink_device,
                "baudrate": self.baudrate,
                "sequence_scope": self.sequence_scope.value if self.sequence_scope else None,
                "system": self.system, "component": self.component}

    def with_sources(self, values):
        """Apply the complete browser form; directories and thresholds stay local."""
        if not isinstance(values, dict) or set(values) != set(self.public()):
            raise ValueError("source configuration must contain exactly the documented fields")
        values = dict(values)
        kind = values.pop("mavlink_transport")
        if kind not in ("none", "udp", "tcp", "serial"):
            raise ValueError("choose none, udp, tcp or serial for MAVLink")
        for key in ("mavlink_bind", "mavlink_peer", "mavlink_tcp"):
            value = values[key]
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("network endpoint must be IPv4:port")
                host, port = value.rsplit(":", 1)
                values[key] = host, int(port)
        scope = values["sequence_scope"]
        values["sequence_scope"] = SequenceScope(scope) if scope is not None else None
        result = replace(self, **values)
        if result.public()["mavlink_transport"] != kind:
            raise ValueError("MAVLink fields do not match the selected transport")
        return result
