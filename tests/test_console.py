"""Passive console API: missing/stale data stay explicit across HTTP reads."""
from collections import deque
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
from fastapi.testclient import TestClient

from argos.backends.mavlink import MavlinkLink, SequenceScope
from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.session import ConsoleSession
from argos.console.video import VideoStore


class Input:
    datagram = False

    def __init__(self):
        self.chunks = deque()
        self.closed = False
        self.reads = 0

    def read(self):
        self.reads += 1
        value = self.chunks.popleft() if self.chunks else b""
        if isinstance(value, Exception):
            raise value
        return value

    def write(self, _):
        raise AssertionError("console must never send MAVLink")

    def close(self):
        self.closed = True


def pack(message, *, component=1, sequence=0):
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=component)
    encoder.seq = sequence
    return bytes(message.pack(encoder))


def configured(**kwargs):
    return ConsoleConfig(environment="simulation", mavlink_bind=("127.0.0.1", 0),
                         mavlink_peer=("127.0.0.1", 14550),
                         sequence_scope=SequenceScope.COMPONENT, **kwargs)


def test_unconfigured_http_has_no_fabricated_telemetry_or_image():
    session = ConsoleSession(ConsoleConfig(), clock=lambda: 0.)
    with TestClient(create_app(session=session)) as client:
        response = client.get("/api/state")
        assert response.status_code == 200
        state = response.json()
        assert state["environment"] == "unconfigured"
        assert state["video"]["state"] == state["telemetry"]["state"] == "unconfigured"
        for name in ("heartbeat", "attitude", "local_position_ned"):
            assert state["telemetry"][name]["fields"] is None
            assert state["telemetry"][name]["rx_age_s"] is None
        assert client.get("/api/frame.jpg").status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_receipts_and_source_selection_survive_http_reads_without_polling_or_refresh():
    now = [0.]
    source = Input()
    wire = MavlinkLink(source, sequence_scope=SequenceScope.COMPONENT)
    session = ConsoleSession(configured(), clock=lambda: now[0], link_factory=lambda: wire)
    source.chunks.append(pack(mav.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3), component=42))
    session.start()
    assert session.state()["telemetry"]["state"] == "waiting"
    assert session.state()["telemetry"]["ignored_source"] == 1
    now[0] = .1
    source.chunks.append(pack(mav.MAVLink_attitude_message(1, .1, .2, .3, 0., 0., 0.)))
    session.tick()
    reads = source.reads
    now[0] = .2
    assert session.state()["telemetry"]["attitude"]["rx_age_s"] == pytest.approx(.1)
    now[0] = 1.
    state = session.state()
    assert state["telemetry"]["state"] == "stale"
    assert state["telemetry"]["attitude"]["rx_age_s"] == pytest.approx(.9)
    assert state["telemetry"]["attitude"]["fields"]["roll"] == pytest.approx(.1)
    assert source.reads == reads  # display reads never become acquisitions
    session.close()
    assert source.closed


def test_invalid_payload_does_not_replace_telemetry_or_break_json_and_error_stays_visible():
    now = [0.]
    source = Input()
    wire = MavlinkLink(source, sequence_scope=SequenceScope.COMPONENT)
    session = ConsoleSession(configured(), clock=lambda: now[0], link_factory=lambda: wire)
    source.chunks.append(pack(mav.MAVLink_attitude_message(1, .1, 0., 0., 0., 0., 0.)))
    session.start()
    now[0] = .1
    source.chunks.append(pack(mav.MAVLink_attitude_message(2, float("nan"), 0., 0., 0., 0., 0.)))
    session.tick()
    telemetry = session.state()["telemetry"]
    assert telemetry["rejected"] == 1
    assert telemetry["attitude"]["fields"]["time_boot_ms"] == 1
    assert telemetry["attitude"]["received_at"] == 0.
    source.chunks.append(OSError("device disconnected"))
    session.tick()
    assert session.state()["telemetry"]["state"] == "error"
    assert "device disconnected" in session.state()["telemetry"]["detail"]
    assert source.closed
    session.close()


def test_open_failure_remains_inspectable_in_the_ui():
    def fail():
        raise OSError("port occupied")
    session = ConsoleSession(configured(), link_factory=fail)
    with TestClient(create_app(session=session)) as client:
        state = client.get("/api/state").json()
        assert state["telemetry"]["state"] == "error"
        assert "port occupied" in state["telemetry"]["detail"]
        assert client.get("/api/frame.jpg").status_code == 503


def test_image_http_headers_preserve_acquisition_and_expiry():
    now = [0.]
    session = ConsoleSession(ConsoleConfig(), clock=lambda: now[0])
    # Inject an acquired image in the test only; the application has no fixture source.
    session.video = VideoStore(source="gazebo", endpoint="/camera", clock=lambda: now[0])
    assert session.video.accept_raw(width=2, height=1, step=6, pixel_format="RGB_INT8",
                                    data=bytes([255, 0, 0, 0, 255, 0]), received_at=0.)
    with TestClient(create_app(session=session)) as client:
        response = client.get("/api/frame.jpg")
        assert response.status_code == 200 and response.content.startswith(b"\xff\xd8")
        assert response.headers["x-frame-sequence"] == "1"
        assert response.headers["x-frame-received-at"] == "0.0"
        assert response.headers["x-run-id"] == session.run_id
        assert response.headers["cache-control"] == "no-store"
        now[0] = .5
        again = client.get("/api/frame.jpg")
        assert again.content == response.content
        assert again.headers["x-frame-received-at"] == "0.0"
        now[0] = 1.01
        assert client.get("/api/frame.jpg").status_code == 503
        assert client.get("/api/state").json()["video"]["state"] == "stale"


def test_static_assets_are_local_and_action_routes_do_not_exist():
    with TestClient(create_app()) as client:
        for path, mime in (("/", "text/html"), ("/app.css", "text/css"),
                           ("/app.js", "text/javascript")):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(mime)
        assert client.post("/api/state", json={"command": "anything"}).status_code == 405
        assert client.post("/api/command").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/app.css", headers={"host": "untrusted.example"}).status_code == 400


def test_fonts_are_served_locally_without_exposing_other_files():
    names = (
        "ibm-plex-sans-400-500-latin.woff2", "ibm-plex-sans-400-500-latin-ext.woff2",
        "ibm-plex-mono-400-latin.woff2", "ibm-plex-mono-400-latin-ext.woff2",
        "ibm-plex-mono-500-latin.woff2", "ibm-plex-mono-500-latin-ext.woff2",
        "marcellus-400-latin.woff2", "marcellus-400-latin-ext.woff2",
    )
    with TestClient(create_app()) as client:
        for name in names:
            response = client.get(f"/fonts/{name}")
            assert response.status_code == 200
            assert response.headers["content-type"] == "font/woff2"
            assert response.content.startswith(b"wOF2")
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "font-src 'self'" in response.headers["content-security-policy"]
        for path in ("/fonts/unknown.woff2", "/fonts/app.py", "/fonts/README.md",
                     "/fonts/%2e%2e%2fapp.py", "/fonts/%2e%2e%5capp.py",
                     "/fonts/%252e%252e%252fapp.py"):
            assert client.get(path).status_code == 404


@pytest.mark.parametrize("kwargs", [
    {"video_source": "file", "video_endpoint": "clip.mp4"},
    {"video_source": "device", "video_endpoint": "https://example.org/clip.mp4", "environment": "real"},
    {"video_source": "gazebo", "video_endpoint": "/camera", "environment": "real"},
    {"video_source": "device", "video_endpoint": "/dev/video0", "environment": "simulation"},
    {"mavlink_bind": ("127.0.0.1", 0)},
    {"mavlink_bind": ("127.0.0.1", 0), "mavlink_peer": ("127.0.0.1", 14550)},
    {"video_age": 0.}, {"video_age": float("nan")}, {"system": 0},
])
def test_config_refuses_ambiguous_sources_and_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        ConsoleConfig(**kwargs)


def test_event_history_is_bounded_and_does_not_repeat_unchanged_states():
    session = ConsoleSession(ConsoleConfig(), clock=lambda: 0.)
    session.start()
    before = session.state()["events"]
    for _ in range(100):
        session.tick()
    assert session.state()["events"] == before
    for count in range(80):
        session._event(0., "info", str(count))
    assert len(session.state()["events"]) == 60
    assert session.state()["events"][-1]["message"] == "79"
    session.close()


def test_console_config_and_help_do_not_import_optional_camera_or_web_dependencies():
    code = ("import sys; import argos.console.config; "
            "assert all(n not in sys.modules for n in ('cv2', 'gz', 'PIL', 'pymavlink', 'fastapi'))")
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)
    result = subprocess.run([sys.executable, "-m", "argos.console", "--help"],
                            check=True, capture_output=True, text=True)
    assert "--gazebo-topic" in result.stdout and "--camera-device" in result.stdout
