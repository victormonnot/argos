"""Launch the local observation HUD: python -m argos.console --help."""
import argparse
import math
from pathlib import Path

from argos.backends.mavlink import SequenceScope, TelemetryLimits
from .config import ConsoleConfig, default_recordings_dir


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return number


def address(value):
    try:
        host, port = value.rsplit(":", 1)
        return host, int(port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected IPv4:port") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    camera = parser.add_mutually_exclusive_group()
    camera.add_argument("--gazebo-topic", help="exact onboard Gazebo Image topic")
    camera.add_argument("--camera-device", help="physical Linux camera, /dev/videoN")
    parser.add_argument("--gazebo-python-path", type=Path,
                        help="explicit directory containing system gz Python bindings")
    parser.add_argument("--environment", choices=("simulation", "real"),
                        help="required for telemetry-only sessions; inferred from camera otherwise")
    parser.add_argument("--sim-control", action="store_true",
                        help="enable manual web controls for the verified GPS-free loopback SITL profile")
    parser.add_argument("--video-age", type=positive, default=1.)
    parser.add_argument("--mavlink-bind", type=address)
    parser.add_argument("--mavlink-peer", type=address)
    parser.add_argument("--mavlink-tcp", type=address, help="read a TCP peer, e.g. SITL 127.0.0.1:5760")
    parser.add_argument("--mavlink-device")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--sequence-scope", choices=[s.value for s in SequenceScope])
    parser.add_argument("--system", type=int, default=1)
    parser.add_argument("--component", type=int, default=1)
    parser.add_argument("--heartbeat-age", type=positive, default=1.)
    parser.add_argument("--attitude-age", type=positive, default=.2)
    parser.add_argument("--position-age", type=positive, default=.4)
    parser.add_argument("--battery-age", type=positive, default=2.)
    parser.add_argument("--recordings-dir", type=Path, default=default_recordings_dir(),
                        help="capture directory (default: XDG_DATA_HOME/argos/recordings or ~/.local/share/argos/recordings)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    source = "gazebo" if args.gazebo_topic else "device" if args.camera_device else "none"
    environment = args.environment or {"gazebo": "simulation", "device": "real", "none": "unconfigured"}[source]
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError("HTTP port must be in 1..65535")
        config = ConsoleConfig(
            sim_control=args.sim_control,
            video_source=source, video_endpoint=args.gazebo_topic or args.camera_device,
            environment=environment, gazebo_python_path=args.gazebo_python_path,
            video_age=args.video_age, mavlink_bind=args.mavlink_bind,
            mavlink_peer=args.mavlink_peer, mavlink_tcp=args.mavlink_tcp,
            mavlink_device=args.mavlink_device,
            baudrate=args.baudrate,
            sequence_scope=SequenceScope(args.sequence_scope) if args.sequence_scope else None,
            system=args.system, component=args.component,
            battery_age=args.battery_age, recordings_dir=args.recordings_dir,
            limits=TelemetryLimits(args.heartbeat_age, args.attitude_age, args.position_age))
        from .app import create_app
        import uvicorn
        app = create_app(config)
    except (ImportError, ValueError, TypeError) as exc:
        parser.exit(2, f"error: {exc}\nInstall: pip install -e '.[console,mavlink]'\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
