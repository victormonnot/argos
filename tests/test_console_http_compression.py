"""Compression changes wire size, never control payloads or lease semantics."""
import asyncio
import gzip
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from starlette.datastructures import Headers

from argos.console.http_compression import ConsoleJSONCompression
from argos.console.video import VideoStore
from test_console_flight_api import ORIGIN, flight


def wire_reply(path, encoding, body=None, extra_headers=()):
    body = body if body is not None else json.dumps({"value": "state " * 1000}).encode()
    request_headers = [(b"accept-encoding", value.encode()) for value in encoding]
    scope = {"type": "http", "path": path, "headers": request_headers}
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"), (b"vary", b"Origin"), *extra_headers]})
        await send({"type": "http.response.body", "body": body})

    async def send(message):
        messages.append(message)

    asyncio.run(ConsoleJSONCompression(app)(scope, None, send))
    assert scope["headers"] is request_headers  # No mutation of the request shared with outer middleware.
    return Headers(raw=messages[0]["headers"]), b"".join(m.get("body", b"") for m in messages), body


@pytest.mark.parametrize("encoding", [
    ("gzip",), ("br, gzip, deflate",), ("GZip;Q=1",), ("*;q=0.5",),
    ("gzip;q=0.5, identity;q=0.2",), ("gzip;q=0.8", "br"),
])
@pytest.mark.parametrize("path", ["/api/state", "/api/control/input", "/api/control/action"])
def test_negotiated_gzip_is_exact_and_has_wire_length_and_vary(path, encoding):
    headers, wire, original = wire_reply(path, encoding)
    assert headers["content-encoding"] == "gzip"
    assert gzip.decompress(wire) == original
    assert int(headers["content-length"]) == len(wire) < len(original)
    assert set(headers["vary"].split(", ")) == {"Origin", "Accept-Encoding"}
    assert headers["cache-control"] == "no-store"


@pytest.mark.parametrize("encoding", [
    (), ("",), ("identity",), ("br, deflate",), ("xgzip",), ("gzip;q=0",),
    ("gzip;q=0, *;q=1",), ("*;q=0",), ("gzip, identity",),
    ("gzip;q=0.4, identity;q=0.8",), ("gzip;q=no",), ("gzip;q=1.1",),
    ("gzip;q=0.1234",), ("gzip;unknown=1",), ("gzip", "gzip;q=0"),
])
def test_excluded_unsupported_or_less_preferred_gzip_stays_identity(encoding):
    headers, wire, original = wire_reply("/api/state", encoding)
    assert "content-encoding" not in headers
    assert wire == original and int(headers["content-length"]) == len(wire)
    assert "Accept-Encoding" in headers["vary"]


@pytest.mark.parametrize("path", ["/api/frame.jpg", "/api/vision/frame.jpg", "/app.js", "/api/recordings/a/export", "/api/state-extra"])
def test_other_paths_bypass_compression(path):
    headers, wire, original = wire_reply(path, ("gzip",))
    assert wire == original
    assert "content-encoding" not in headers and headers["vary"] == "Origin"


def test_small_or_preencoded_replies_keep_standard_passthrough():
    headers, wire, _ = wire_reply("/api/control/input", ("gzip",), b'{"detail":"Rejected"}')
    assert "content-encoding" not in headers and wire == b'{"detail":"Rejected"}'
    headers, wire, original = wire_reply("/api/state", ("gzip",), extra_headers=[(b"content-encoding", b"br")])
    assert headers["content-encoding"] == "br" and wire == original


def test_real_state_and_control_keep_schemas_security_and_expiry(flight):
    client, session, wire, now = flight
    identity = client.get("/api/state", headers={"accept-encoding": "identity"})
    compressed = client.get("/api/state", headers={"accept-encoding": "gzip"})
    assert compressed.json() == identity.json()
    assert compressed.headers["content-encoding"] == "gzip"
    assert int(compressed.headers["content-length"]) < len(compressed.content)
    for key in ("cache-control", "content-security-policy", "x-content-type-options", "referrer-policy"):
        assert compressed.headers[key] == identity.headers[key]
    assert compressed.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in compressed.headers["content-security-policy"]
    request_headers = {**ORIGIN, "accept-encoding": "gzip"}
    claim = client.post("/api/control/claim", json={}, headers=request_headers)
    assert claim.headers["content-encoding"] == "gzip"
    token = claim.json()["token"]
    now[0] = .1
    body = {"token": token, "seq": 1, "mode_generation": 0, "axes": dict(forward=0, right=0, up=0, yaw=0)}
    reply = client.post("/api/control/input", json=body, headers=request_headers)
    assert reply.status_code == 200 and reply.headers["content-encoding"] == "gzip"
    assert reply.json()["control"]["input_seq"] == 1
    now[0] = .5
    conflict = client.post("/api/control/input", json={**body, "seq": 2, "mode_generation": 1}, headers=request_headers)
    assert conflict.status_code == 409 and conflict.headers["content-encoding"] == "gzip"
    assert conflict.json()["code"] == "stale_mode_generation"
    assert conflict.json()["control"]["input_seq"] == 1
    now[0] = .74
    state = client.get("/api/state", headers={"accept-encoding": "gzip"}).json()["control"]
    assert state["owned"] and state["last_input_age"] == pytest.approx(.64)
    now[0] = .76
    session.tick()
    state = client.get("/api/state", headers={"accept-encoding": "gzip"}).json()["control"]
    assert not state["owned"] and state["phase"] == "expired"


def test_real_images_and_static_are_not_recompressed(flight):
    client, session, wire, now = flight
    session.video = VideoStore(source="gazebo", endpoint="/camera", clock=lambda: now[0])
    assert session.video.accept_raw(width=2, height=1, step=6, pixel_format="RGB_INT8",
                                    data=bytes([255, 0, 0, 0, 255, 0]), received_at=0.)
    for path in ("/api/frame.jpg", "/app.js"):
        response = client.get(path, headers={"accept-encoding": "gzip"})
        assert response.status_code == 200 and len(response.content) > 500
        assert "content-encoding" not in response.headers
        assert int(response.headers["content-length"]) == len(response.content)
    assert client.get("/api/frame.jpg").headers["x-frame-received-at"] == "0.0"
