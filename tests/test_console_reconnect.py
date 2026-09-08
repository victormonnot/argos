"""Passive receiver recovery stays isolated, cancellable and observable."""
import asyncio
from collections import deque
from dataclasses import replace
from threading import Event, get_ident

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
from fastapi.testclient import TestClient

from argos.backends.mavlink import MavlinkLink, SequenceScope
from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.session import ConsoleSession
import argos.console.session as session_module


ORIGIN = {"origin": "http://testserver"}


class Input:
    datagram = False

    def __init__(self):
        self.chunks = deque()
        self.closed = False
        self.close_thread = None
        self.reads = 0

    def read(self):
        assert not self.closed
        self.reads += 1
        value = self.chunks.popleft() if self.chunks else b""
        if isinstance(value, Exception):
            raise value
        return value

    def write(self, _):
        raise AssertionError("receiver recovery must never transmit MAVLink")

    def close(self):
        self.closed = True
        self.close_thread = get_ident()


class Camera:
    def __init__(self, store, **_):
        self.store = store
        self.closed = False
        self.close_thread = None

    def start(self):
        pass

    def close(self):
        self.closed = True
        self.close_thread = get_ident()
        self.store.stop()


def wire(source):
    return MavlinkLink(source, sequence_scope=SequenceScope.COMPONENT)


def heartbeat(*, component=1, sequence=0):
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=component)
    encoder.seq = sequence
    return bytes(mav.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3).pack(encoder))


def image(store, at):
    return store.accept_raw(width=1, height=1, step=3, pixel_format="RGB_INT8",
                            data=b"\x01\x02\x03", received_at=at)


def configured(directory):
    return ConsoleConfig(environment="simulation", video_source="gazebo", video_endpoint="/camera",
                         mavlink_bind=("127.0.0.1", 0), mavlink_peer=("127.0.0.1", 14550),
                         sequence_scope=SequenceScope.COMPONENT, recordings_dir=directory)


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "GazeboCamera", Camera)
    now, inputs = [0.], []

    def factory():
        source = Input()
        inputs.append(source)
        return wire(source)

    session = ConsoleSession(configured(tmp_path), clock=lambda: now[0], link_factory=factory)
    session.start()
    return session, now, inputs


def post(client, path, body=None):
    return client.post(path, json={} if body is None else body, headers=ORIGIN)


def test_mavlink_reopen_keeps_run_camera_and_completed_journal(tmp_path, monkeypatch):
    session, now, inputs = setup(tmp_path, monkeypatch)
    client = TestClient(create_app(session=session))
    try:
        inputs[0].chunks.append(heartbeat())
        assert image(session.video, 0.)
        session.tick()
        post(client, "/api/recordings/start")
        now[0] = .1
        completed = post(client, "/api/recordings/stop").json()
        contents = client.get(completed["download_url"]).content
        before = session.state()
        camera, video = session.camera, session.video
        response = post(client, "/api/sources/mavlink/reconnect")
        assert response.status_code == 200
        after = response.json()
        assert after["run_id"] == before["run_id"] and after["at"] == .1
        assert after["telemetry"]["connection_id"] != before["telemetry"]["connection_id"]
        assert after["video"]["source_id"] == before["video"]["source_id"]
        assert session.camera is camera and session.video is video and not camera.closed
        assert inputs[0].closed and len(inputs) == 2
        assert after["telemetry"]["state"] == "waiting"
        assert after["telemetry"]["heartbeat"]["fields"] is None
        assert after["telemetry"]["rx_messages"] == 0
        assert after["recording"] == completed
        assert client.get(completed["download_url"]).content == contents
        assert client.get("/api/mavlink/messages").json()["types"] == []
        inputs[1].chunks.append(heartbeat())
        now[0] = .2
        session.tick()
        assert session.state()["telemetry"]["state"] == "receiving"
    finally:
        session.close()
        client.close()


def test_video_reopen_keeps_active_capture_and_identifies_new_frame_generation(tmp_path, monkeypatch):
    session, now, inputs = setup(tmp_path, monkeypatch)
    client = TestClient(create_app(session=session))
    try:
        assert image(session.video, 0.)
        old_frame = client.get("/api/frame.jpg")
        old_store, old_camera, link = session.video, session.camera, session.link
        before = session.state()
        started = post(client, "/api/recordings/start").json()
        now[0] = .1
        response = post(client, "/api/sources/video/reconnect")
        assert response.status_code == 200
        state = response.json()
        assert state["run_id"] == before["run_id"]
        assert state["video"]["source_id"] != before["video"]["source_id"]
        assert state["telemetry"]["connection_id"] == before["telemetry"]["connection_id"]
        assert state["video"]["state"] == "waiting"
        assert state["recording"]["id"] == started["id"] and session.recorder.active
        assert session.link is link and not inputs[0].closed
        assert old_camera.closed and not image(old_store, .1)
        assert client.get("/api/frame.jpg").status_code == 503
        assert image(session.video, .1)
        frame = client.get("/api/frame.jpg")
        assert frame.headers["x-frame-sequence"] == old_frame.headers["x-frame-sequence"] == "1"
        assert frame.headers["x-video-id"] != old_frame.headers["x-video-id"]
        assert frame.headers["x-video-id"] == state["video"]["source_id"]
        assert frame.headers["x-run-id"] == old_frame.headers["x-run-id"]
        inputs[0].chunks.append(heartbeat())
        session.tick()
        assert session.recorder.snapshot()["events"] == 1
    finally:
        session.close()
        client.close()


def test_active_recording_blocks_only_mavlink_reopen_without_side_effects(tmp_path, monkeypatch):
    session, _now, inputs = setup(tmp_path, monkeypatch)
    client = TestClient(create_app(session=session))
    try:
        post(client, "/api/recordings/start")
        before = session.state()
        response = post(client, "/api/sources/mavlink/reconnect")
        assert response.status_code == 409 and "recording" in response.json()["detail"]
        assert len(inputs) == 1 and not inputs[0].closed
        assert session.state()["telemetry"]["connection_id"] == before["telemetry"]["connection_id"]
        assert session.recorder.active
    finally:
        session.close()
        client.close()


def test_failed_reopen_remains_visible_and_does_not_restart_failed_journal(tmp_path, monkeypatch):
    session, now, inputs = setup(tmp_path, monkeypatch)
    client = TestClient(create_app(session=session))
    try:
        started = post(client, "/api/recordings/start").json()
        inputs[0].chunks.append(ConnectionError("input disconnected"))
        now[0] = 1.
        session.tick()
        failed_recording = session.recorder.snapshot()

        def fail():
            raise OSError("port unavailable")

        session._link_factory = fail
        response = post(client, "/api/sources/mavlink/reconnect")
        assert response.status_code == 200
        state = response.json()
        assert state["telemetry"]["state"] == "error" and "port unavailable" in state["telemetry"]["detail"]
        assert state["reconnecting"] is None
        assert state["recording"] == failed_recording
        assert state["recording"]["id"] == started["id"]
        replacement = Input()
        session._link_factory = lambda: wire(replacement)
        assert post(client, "/api/sources/mavlink/reconnect").json()["telemetry"]["state"] == "waiting"
        assert session.recorder.snapshot() == failed_recording
        assert post(client, "/api/recordings/start").json()["id"] != started["id"]
    finally:
        session.close()
        client.close()


@pytest.mark.parametrize("source", ["video", "mavlink"])
@pytest.mark.parametrize("headers", [{}, {"origin": "https://other.example"}])
def test_reconnect_requires_local_origin_before_changes(tmp_path, monkeypatch, source, headers):
    session, _now, inputs = setup(tmp_path, monkeypatch)
    client = TestClient(create_app(session=session))
    try:
        before = session.state()
        assert client.post(f"/api/sources/{source}/reconnect", json={}, headers=headers).status_code == 403
        assert session.state() == before and len(inputs) == 1
    finally:
        session.close()
        client.close()


def test_reconnect_validates_exact_body_source_and_configuration(tmp_path):
    session = ConsoleSession(ConsoleConfig(recordings_dir=tmp_path))
    with TestClient(create_app(session=session)) as client:
        assert post(client, "/api/sources/video/reconnect").status_code == 409
        assert post(client, "/api/sources/mavlink/reconnect").status_code == 409
        assert post(client, "/api/sources/unknown/reconnect").status_code == 404
        assert post(client, "/api/sources/video/reconnect", []).status_code == 422
        assert post(client, "/api/sources/mavlink/reconnect", {"endpoint": "elsewhere"}).status_code == 422


def test_blocked_link_open_keeps_http_video_live_and_rejects_conflicting_writes(tmp_path, monkeypatch):
    session, now, inputs = setup(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    replacement = Input()

    def blocking_factory():
        entered.set()
        assert release.wait(3.)
        return wire(replacement)

    session._link_factory = blocking_factory

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(session=session)),
                                     base_url="http://testserver") as client:
            pending = asyncio.create_task(client.post("/api/sources/mavlink/reconnect", json={}, headers=ORIGIN))
            try:
                assert await asyncio.to_thread(entered.wait, 1.)
                assert inputs[0].close_thread != get_ident()
                now[0] = .5
                assert image(session.video, .5)
                session.tick()
                state = (await asyncio.wait_for(client.get("/api/state"), .5)).json()
                assert state["reconnecting"] == "mavlink"
                assert state["telemetry"]["state"] == "reconnecting"
                assert state["video"]["state"] == "recent" and state["at"] == .5
                assert (await client.get("/api/frame.jpg")).status_code == 200
                for path, body in (("/api/recordings/start", {}),
                                   ("/api/sources/video/reconnect", {}),
                                   ("/api/sources/mavlink/reconnect", {}),
                                   ("/api/sources", session.config.public())):
                    assert (await client.post(path, json=body, headers=ORIGIN)).status_code == 409
            finally:
                release.set()
                result = await pending
            assert result.status_code == 200 and result.json()["reconnecting"] is None
            assert not replacement.closed

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        session.close()


def test_blocked_camera_close_keeps_mavlink_receiving_and_capture_available(tmp_path, monkeypatch):
    session, now, inputs = setup(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    previous = session.camera
    original_close = previous.close

    def blocking_close():
        entered.set()
        assert release.wait(3.)
        original_close()

    previous.close = blocking_close

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(session=session)),
                                     base_url="http://testserver") as client:
            pending = asyncio.create_task(client.post("/api/sources/video/reconnect", json={}, headers=ORIGIN))
            try:
                assert await asyncio.to_thread(entered.wait, 1.)
                now[0] = .5
                inputs[0].chunks.append(heartbeat())
                session.tick()
                state = (await asyncio.wait_for(client.get("/api/state"), .5)).json()
                assert state["video"]["state"] == "reconnecting"
                assert state["telemetry"]["state"] == "receiving"
                assert (await client.get("/api/frame.jpg")).status_code == 503
                assert (await client.post("/api/recordings/start", json={}, headers=ORIGIN)).status_code == 200
            finally:
                release.set()
                result = await pending
            assert result.json()["recording"]["state"] == "recording"
            assert previous.close_thread != get_ident()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        session.close()


@pytest.mark.parametrize("close_session", [False, True])
def test_cancelled_request_cannot_abandon_opened_link_or_unlock_early(tmp_path, monkeypatch, close_session):
    session, _now, _inputs = setup(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    replacement = Input()

    def factory():
        entered.set()
        assert release.wait(3.)
        return wire(replacement)

    session._link_factory = factory

    async def scenario():
        request = asyncio.create_task(session.reconnect("mavlink"))
        try:
            assert await asyncio.to_thread(entered.wait, 1.)
            operation = session._reconnect_task
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert session.reconnecting == "mavlink" and not operation.done()
            if close_session:
                session.close()
            else:
                with pytest.raises(RuntimeError, match="already reopening"):
                    await session.reconnect("mavlink")
        finally:
            release.set()
            await operation
        assert session.reconnecting is None
        if close_session:
            assert replacement.closed and session.link is None
        else:
            assert not replacement.closed and session.link is not None

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        session.close()


def test_blocked_physical_reader_cannot_accumulate_on_retries_or_settings_changes(tmp_path, monkeypatch):
    created = []

    class Device(Camera):
        def __init__(self, store):
            super().__init__(store)
            self.worker_stopped = False
            created.append(self)

    monkeypatch.setattr(session_module, "DeviceCamera", Device)
    config = replace(configured(tmp_path), environment="real", video_source="device", video_endpoint="/dev/video0")
    session = ConsoleSession(config, clock=lambda: 0., link_factory=lambda: wire(Input()))
    session.start()
    client = TestClient(create_app(session=session))
    try:
        original = session.camera
        for _ in range(3):
            state = post(client, "/api/sources/video/reconnect").json()
            assert state["video"]["state"] == "error" and "released" in state["video"]["detail"]
            assert len(created) == 1 and session.camera is original
        assert post(client, "/api/sources", session.config.public()).status_code == 409
        assert len(created) == 1
        original.worker_stopped = True
        assert post(client, "/api/sources/video/reconnect").json()["video"]["state"] == "waiting"
        assert len(created) == 2 and session.camera is not original
    finally:
        session.close()
        client.close()


def test_live_endpoint_is_passive_keeps_rejected_fields_and_expires_rates(tmp_path, monkeypatch):
    session, now, inputs = setup(tmp_path, monkeypatch)
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    malformed_payload = bytes(mav.MAVLink_attitude_message(1, float("nan"), 0., 0., 0., 0., 0.).pack(encoder))
    inputs[0].chunks.append(heartbeat(component=42) + malformed_payload)
    session.tick()
    client = TestClient(create_app(session=session))
    try:
        reads = inputs[0].reads
        response = client.get("/api/mavlink/messages")
        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == session.run_id and data["connection_id"] == session.connection_id
        assert data["rx_messages"] == 2 and data["rx_bytes"] > 0
        assert {(row["component"], row["type_name"]) for row in data["types"]} == {(42, "HEARTBEAT"), (1, "ATTITUDE")}
        attitude = next(row for row in data["types"] if row["type_name"] == "ATTITUDE")
        assert attitude["fields"]["roll"] == "NaN"
        assert attitude["frame_hex"] == malformed_payload.hex()
        assert session.state()["telemetry"]["rejected"] == 1
        assert session.state()["telemetry"]["ignored_source"] == 1
        now[0] = 4.
        expired = client.get("/api/mavlink/messages").json()
        assert all(row["hz"] == 0. and row["rx_age_s"] == 4. for row in expired["types"])
        assert inputs[0].reads == reads
        assert response.headers["cache-control"] == "no-store"
    finally:
        session.close()
        client.close()


def test_shutdown_drains_owned_open_before_finishing(tmp_path, monkeypatch):
    session, _now, _inputs = setup(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    replacement = Input()

    def factory():
        entered.set()
        assert release.wait(3.)
        return wire(replacement)

    session._link_factory = factory

    async def scenario():
        request = asyncio.create_task(session.reconnect("mavlink"))
        try:
            assert await asyncio.to_thread(entered.wait, 1.)
            shutdown = asyncio.create_task(session.aclose())
            await asyncio.sleep(0)
            assert session._closed and not shutdown.done()
        finally:
            release.set()
            await request
            await shutdown
        assert replacement.closed and session.link is None

    asyncio.run(scenario())


def test_cancelled_settings_drain_keeps_ownership_and_blocks_recording_until_cancel(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    class Device(Camera):
        worker_stopped = False

        def close(self):
            if self.closed:
                return
            self.closed = True
            self.store.stop()
            entered.set()
            assert release.wait(3.)
            self.worker_stopped = True

    monkeypatch.setattr(session_module, "DeviceCamera", Device)
    config = replace(configured(tmp_path), environment="real", video_source="device", video_endpoint="/dev/video0")
    session = ConsoleSession(config, clock=lambda: 0., link_factory=lambda: wire(Input()))
    session.start()
    app = create_app(session=session)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            request = asyncio.create_task(client.post("/api/sources", json=session.config.public(), headers=ORIGIN))
            try:
                assert await asyncio.to_thread(entered.wait, 1.)
                operation = session._reconnect_task
                assert session.replacing and session.reconnecting == "video"
                assert (await client.post("/api/recordings/start", json={}, headers=ORIGIN)).status_code == 409
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert not session.replacing and session.reconnecting == "video"
                assert app.state.session is session and not session._closed
            finally:
                release.set()
                await operation
            assert session.reconnecting is None and session.camera.worker_stopped
            assert app.state.session is session

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        session.close()


def test_live_endpoint_reports_observed_decode_and_transport_errors(tmp_path, monkeypatch):
    session, _now, inputs = setup(tmp_path, monkeypatch)
    inputs[0].chunks.extend([b"bad", OSError("input interrupted")])
    session.tick()
    client = TestClient(create_app(session=session))
    try:
        data = client.get("/api/mavlink/messages").json()
        assert data["state"] == "error" and data["rx_bytes"] == 3
        assert data["bad_bytes"] == 3 and data["read_errors"] == 1
        assert data["unsupported_frames"] == 0 and data["rx_messages"] == 0
        assert data["types"] == []
    finally:
        session.close()
        client.close()
