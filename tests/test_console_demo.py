"""The public visitor surface reads archives without creating live services."""
from pathlib import Path
import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("pymavlink")
from fastapi.testclient import TestClient

from argos.console import app as module
from argos.console.__main__ import main
from argos.console.config import ConsoleConfig, default_demo_recordings_dir, default_recordings_dir


ID = "05aa147d371144c793c8f88ec3ef0509"


@pytest.fixture
def client():
    with TestClient(module.create_app(ConsoleConfig(demo=True, public_hosts=("try.argos.example",)))) as value:
        yield value


def test_demo_default_never_selects_the_users_archive(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert ConsoleConfig().recordings_dir == default_recordings_dir()
    assert ConsoleConfig(demo=True).recordings_dir == default_demo_recordings_dir()
    assert not tmp_path.joinpath("argos").exists()


def test_demo_never_constructs_or_starts_live_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A public replay must not construct a live service")

    monkeypatch.setattr(module, "ConsoleSession", forbidden)
    monkeypatch.setattr(module, "VisionService", forbidden)
    app = module.create_app(ConsoleConfig(demo=True))
    assert app.state.session is app.state.vision is None
    with TestClient(app) as client:
        assert client.get("/api/demo-config").json() == {"enabled": True, "recording_id": ID}
        assert client.get("/api/recordings").json()["items"][0]["id"] == ID


@pytest.mark.parametrize("values", [
    {"video_source": "device", "video_endpoint": "/dev/video0", "environment": "real"},
    {"mavlink_tcp": ("127.0.0.1", 5760)},
    {"mavlink_device": "/dev/ttyUSB0"},
    {"mavlink_peer": ("127.0.0.1", 14550)},
    {"vision_model": Path("detector.onnx")},
    {"vision_bundle": Path("detector")},
    {"sim_control": True}, {"sim_framing": True}, {"yaw_assist": True},
    {"environment": "simulation"}, {"gazebo_python_path": Path("bindings")},
])
def test_demo_rejects_live_configuration(values):
    with pytest.raises(ValueError, match="cannot configure live"):
        ConsoleConfig(demo=True, **values)


@pytest.mark.parametrize("service", ["session", "vision", "yaw_service"])
def test_demo_rejects_injected_live_services(service):
    with pytest.raises(ValueError, match="cannot accept live services"):
        module.create_app(ConsoleConfig(demo=True), **{service: object()})


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/sources"), ("POST", "/api/sources/video/reconnect"),
    ("POST", "/api/recordings/start"), ("POST", "/api/recordings/stop"),
    ("POST", "/api/control/claim"), ("POST", "/api/vision/yaw-preview"),
    ("PUT", "/api/recordings"), ("DELETE", f"/api/recordings/{ID}"),
    ("PATCH", "/api/sources"), ("OPTIONS", "/api/sources"),
])
def test_demo_refuses_all_mutations_even_from_its_own_origin(client, method, path):
    response = client.request(method, path, json={}, headers={"Origin": "http://testserver"})
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert "read" in response.json()["detail"]


@pytest.mark.parametrize("path", [
    "/api/state", "/api/frame.jpg", "/api/vision/frame.jpg", "/api/mavlink/messages",
    "/api/vision/yaw-assist/state", "/api/recordings/start", "/api/recordings/arbitrary",
    "/api/recordings/../state", "/api/recordings%2F..%2Fstate", "/openapi.json",
])
def test_demo_exposes_no_live_or_unlisted_endpoint(client, path):
    assert client.get(path).status_code == 404


def test_demo_retains_real_archive_replay_filters_analysis_and_downloads(client):
    catalog = client.get("/api/recordings").json()
    assert catalog["directory"] == "Included demo recordings"
    assert str(default_demo_recordings_dir()) not in str(catalog)
    metadata = client.get(f"/api/recordings/{ID}").json()
    query = {"revision": metadata["revision"], "visual_revision": metadata["visual"]["revision"]}
    visual = client.get(f"/api/recordings/{ID}/visual", params={**query, "at": 20})
    assert visual.status_code == 200
    assert visual.json()["frame"]["detections"]
    assert client.get(visual.json()["frame"]["url"]).content.startswith(b"\xff\xd8")
    for operation in ("replay", "messages", "analysis", "framing-report", "download", "visual/download"):
        response = client.get(f"/api/recordings/{ID}/{operation}", params=query)
        assert response.status_code == 200, (operation, response.text[:200])
    messages = client.get(f"/api/recordings/{ID}/messages", params={**query, "message_id": 253})
    assert all(item["message_id"] == 253 for item in messages.json()["items"])
    invalid = client.get(f"/api/recordings/{ID}/visual", params={**query, "at": 1000})
    assert invalid.status_code == 422


def test_demo_restricts_hosts_and_keeps_security_headers(client):
    assert client.get("/api/demo-config", headers={"Host": "try.argos.example"}).status_code == 200
    assert client.get("/api/demo-config", headers={"Host": "unknown.example"}).status_code == 400
    response = client.get("/api/recordings")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("host", ["*", "*.example", "https://example.com", "example.com:8080", "example.com/path", "a..b", "-example.com"])
def test_public_hosts_must_be_explicit_exact_names(host):
    with pytest.raises(ValueError, match="exact hostnames"):
        ConsoleConfig(demo=True, public_hosts=(host,))


def test_local_default_cannot_accept_public_hosts():
    with pytest.raises(ValueError, match="only supported"):
        ConsoleConfig(public_hosts=("example.com",))


def test_local_default_remains_an_observation_console():
    with TestClient(module.create_app()) as client:
        html = client.get("/").text
        assert 'data-demo="true"' not in html
        assert 'id="demo-welcome"' not in html
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/demo-config").json() == {"enabled": False, "recording_id": None}
        assert client.get("/demo").status_code == 404
        assert client.get("/api/state", headers={"Host": "try.argos.example"}).status_code == 400


def test_demo_welcome_overlays_the_real_console_and_direct_entry_skips_it(client):
    welcome = client.get("/")
    assert welcome.status_code == 200
    assert '<body data-demo="true"' in welcome.text
    assert 'id="sessions-workspace"' in welcome.text
    assert 'id="demo-welcome"' in welcome.text
    assert "Try ARGOS" in welcome.text
    assert welcome.text.count("<html") == 1
    console = client.get("/demo")
    assert console.status_code == 200
    assert '<body data-demo="true"' in console.text
    assert 'id="demo-welcome"' not in console.text
    assert 'src="/recorded-view.js"' in console.text
    assert client.get("/recorded-view.js").status_code == 200
    assert client.get("/demo.js").status_code == 200
    assert client.get("/demo.css").status_code == 200


def test_cli_preserves_loopback_default_and_requires_explicit_public_hosts(monkeypatch):
    import uvicorn
    launches = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: launches.append((app, kwargs)))
    monkeypatch.setattr(sys, "argv", ["argos.console"])
    main()
    assert launches[-1][1]["host"] == "127.0.0.1"
    monkeypatch.setattr(sys, "argv", ["argos.console", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    monkeypatch.setattr(sys, "argv", ["argos.console", "--demo", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        main()
    monkeypatch.setattr(sys, "argv", ["argos.console", "--demo", "--host", "0.0.0.0", "--public-host", "try.argos.example", "--port", "8082"])
    main()
    assert launches[-1][1]["host"] == "0.0.0.0"
    assert launches[-1][1]["port"] == 8082
    assert launches[-1][0].state.session is None
