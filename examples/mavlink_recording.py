"""Capture passive telemetry, inspect a journal or export a reception timeline.

    python examples/mavlink_recording.py create-demo /tmp/argos-rx.jsonl
    python examples/mavlink_recording.py inspect /tmp/argos-rx.jsonl
    python examples/mavlink_recording.py plot /tmp/argos-rx.jsonl /tmp/argos-rx.svg

Only capture-udp and capture-serial open a connection; they never transmit MAVLink
messages. Example age limits are for inspection only. Outputs are never replaced.
"""
import argparse
from collections import deque
from dataclasses import asdict
from importlib.metadata import version
import io
import json
import math
from pathlib import Path
import signal
import sys
from threading import Event

from argos.backends.mavlink import (
    CaptureError, MavlinkLink, RecordingWriter, SequenceScope, SerialTransport,
    TelemetryCache, TelemetryLimits, UdpTransport, capture_session, read_recording,
)


class _Input:
    datagram = False

    def __init__(self):
        self.chunks = deque()

    def read(self):
        return self.chunks.popleft() if self.chunks else b""

    def write(self, data):
        raise AssertionError("offline observation never emits a message")

    def close(self):
        pass


def create_demo(path: Path):
    from pymavlink.dialects.v20 import ardupilotmega as mav

    deliveries = [
        (.1, mav.MAVLink_attitude_message(200, .1, .2, .3, 0., 0., 0.)),
        (.1, mav.MAVLink_local_position_ned_message(150, 10., 20., -3., 1., 2., -1.)),
        (.75, mav.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3)),
        (.8, mav.MAVLink_attitude_message(300, math.nan, .2, .3, 0., 0., 0.)),
        (1., mav.MAVLink_attitude_message(200, .1, .2, .3, 0., 0., 0.)),
    ]
    source = _Input()
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    with path.open("xb") as stream, MavlinkLink(
            source, sequence_scope=SequenceScope.COMPONENT) as link:
        writer = RecordingWriter(stream)
        for at, message in deliveries:
            source.chunks.append(message.pack(encoder))
            encoder.seq = (encoder.seq + 1) % 256
            for event in link.poll(at):
                writer.append(event)
        writer.finish(3.)
    return {"created": str(path.resolve()), "events": len(deliveries), "ended_at": 3.}


def inspect(path: Path, *, system: int, component: int, limits: TelemetryLimits,
            max_events: int):
    with path.open("rb") as stream:
        recording = read_recording(stream, max_events=max_events)
    # No partially validated file reaches the cache. This replays receipt dates,
    # not the live consumer's scheduling, which is absent from the journal.
    cache = TelemetryCache(system=system, component=component, limits=limits,
                           started_at=recording.started_at)
    for event in recording.events:
        cache.update(event, event.received_at)
    snapshot = cache.snapshot(recording.ended_at)
    views = {}
    for name in ("heartbeat", "attitude", "local_position_ned"):
        view = getattr(snapshot, name)
        message = view.message
        views[name] = {
            "reception_state": view.state.value,
            "rx_age_s": view.rx_age,
            "received_at": message.received_at if message else None,
            "sequence": message.sequence if message else None,
            "boot_progress": view.boot_progress.value if view.boot_progress else None,
            "fields": dict(message.fields) if message else None,
        }
    return {
        "scope": "offline recorded receptions; reception age is not measurement age",
        "integrity": "journal checksum verified; no sender authentication",
        "recorded_codec_version": recording.codec_version,
        "reader_codec_version": version("pymavlink"),
        "started_at": recording.started_at,
        "ended_at": recording.ended_at,
        "recorded_events": len(recording.events),
        "selected_source": {"system": system, "component": component},
        "diagnostics": {
            "accepted": snapshot.accepted,
            "ignored_source": snapshot.ignored_source,
            "ignored_type": snapshot.ignored_type,
            "rejected": snapshot.rejected,
            "last_rejection": snapshot.last_rejection,
        },
        "telemetry": views,
    }


def address(value):
    try:
        host, port = value.rsplit(":", 1)
        return host, int(port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an IPv4 address and port: 127.0.0.1:14550") from exc


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return number


def capture(args):
    stopped = Event()
    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        with args.path.open("xb") as stream:
            if args.command == "capture-udp":
                transport = UdpTransport(local=args.bind, peer=args.peer)
            else:
                transport = SerialTransport(device=args.device, baudrate=args.baudrate)
            try:
                link = MavlinkLink(transport, sequence_scope=SequenceScope(args.sequence_scope))
            except Exception:
                transport.close()
                raise
            with link:
                endpoint = (f"{transport.local_address[0]}:{transport.local_address[1]}"
                            if args.command == "capture-udp" else args.device)
                print(f"Listening on {endpoint}", file=sys.stderr, flush=True)
                summary = capture_session(link, stream, duration=args.duration,
                                          poll_interval=args.poll_interval,
                                          stop_requested=stopped.is_set, sleep=stopped.wait)
        return {"recording": str(args.path.resolve()), **asdict(summary)}
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def add_inspection_options(parser):
    parser.add_argument("--system", type=int, default=1)
    parser.add_argument("--component", type=int, default=1)
    parser.add_argument("--heartbeat-age", type=positive, default=1.)
    parser.add_argument("--attitude-age", type=positive, default=.2)
    parser.add_argument("--position-age", type=positive, default=.4)
    parser.add_argument("--max-events", type=int, default=100_000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-demo", help="create a sample file without replacing one")
    create.add_argument("path", type=Path)
    replay = commands.add_parser("inspect", help="validate and inspect a journal entirely offline")
    replay.add_argument("path", type=Path)
    add_inspection_options(replay)
    plot = commands.add_parser("plot", help="export an offline reception timeline as SVG or PNG")
    plot.add_argument("path", type=Path)
    plot.add_argument("output", type=Path)
    add_inspection_options(plot)
    for name in ("capture-udp", "capture-serial"):
        live = commands.add_parser(name, help="record incoming messages without transmitting")
        live.add_argument("path", type=Path)
        live.add_argument("--duration", type=positive, required=True)
        live.add_argument("--poll-interval", type=positive, default=.01)
        live.add_argument("--sequence-scope", choices=[scope.value for scope in SequenceScope],
                          required=True)
        if name == "capture-udp":
            live.add_argument("--bind", type=address, required=True)
            live.add_argument("--peer", type=address, required=True)
        else:
            live.add_argument("--device", required=True)
            live.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()
    try:
        if args.command == "create-demo":
            result = create_demo(args.path)
        elif args.command.startswith("capture-"):
            result = capture(args)
        else:
            limits = TelemetryLimits(args.heartbeat_age, args.attitude_age, args.position_age)
            if args.command == "plot":
                from argos.harness.telemetry_plot import plot_recording
                format = args.output.suffix.lower().lstrip(".")
                if format not in ("svg", "png"):
                    raise ValueError("plot output must end in .svg or .png")
                with args.path.open("rb") as stream:
                    recording = read_recording(stream, max_events=args.max_events)
                if args.output.exists():
                    raise FileExistsError(f"output already exists: {args.output}")
                rendered = io.BytesIO()
                plot_recording(recording, rendered, system=args.system,
                               component=args.component, limits=limits, format=format)
                with args.output.open("xb") as stream:
                    stream.write(rendered.getvalue())
                result = {"plot": str(args.output.resolve()), "recorded_events": len(recording.events)}
            else:
                result = inspect(args.path, system=args.system, component=args.component,
                                 limits=limits, max_events=args.max_events)
    except (CaptureError, OSError, ImportError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
