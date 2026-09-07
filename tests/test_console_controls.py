"""Console source changes and user-controlled passive telemetry journals."""
from collections import deque
from contextlib import contextmanager
import io

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
from fastapi.testclient import TestClient

from argos.backends.mavlink import MavlinkLink, RecordingError, SequenceScope, read_recording
from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.recording import ConsoleRecorder
from argos.console.session import ConsoleSession


ORIGIN = {"origin": "http://testserver"}


class MemoryInput:
    datagram = False

    def __init__(self):
        self.chunks = deque()
        self.closed = False

    def read(self):
        item = self.chunks.popleft() if self.chunks else b""
        if isinstance(item, Exception):
            raise item
        return item

    def write(self, _):
        raise AssertionError("observation controls must not transmit MAVLink")

    def close(self):
        self.closed = True


def frame(message, *, component=1, sequence=0):
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=component)
    encoder.seq = sequence
    return bytes(message.pack(encoder))


def configured(directory):
    return ConsoleConfig(environment="simulation", mavlink_bind=("127.0.0.1", 0),
                         mavlink_peer=("127.0.0.1", 14550),
                         sequence_scope=SequenceScope.COMPONENT, recordings_dir=directory)


@contextmanager
def controlled_client(config, *, real_udp=False):
    # Drive acquisitions and run time explicitly; TestClient without a lifespan
    # avoids racing the usual 50 ms receiver while injecting error conditions.
    now = [0.]
    source = MemoryInput()
    options = {} if real_udp else {"link_factory": lambda: MavlinkLink(
        source, sequence_scope=SequenceScope.COMPONENT)}
    session = ConsoleSession(config, clock=lambda: now[0], **options)
    session.start()
    app = create_app(session=session)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, app, session, source, now
    finally:
        app.state.session.close()
        client.close()


def post(client, path, values):
    return client.post(path, json=values, headers=ORIGIN)


def test_journal_preserves_original_frames_before_source_and_payload_filtering(tmp_path):
    with controlled_client(configured(tmp_path)) as (client, app, session, source, now):
        started = post(client, "/api/recordings/start", {})
        assert started.status_code == 200 and started.json()["state"] == "recording"
        frames = [frame(mav.MAVLink_heartbeat_message(2, 3, 1, 0, 4, 3), component=42),
                  frame(mav.MAVLink_attitude_message(1, float("nan"), 0., 0., 0., 0., 0.),
                        sequence=1)]
        source.chunks.append(b"".join(frames))
        now[0] = 1.
        session.tick()
        state = client.get("/api/state").json()
        assert state["recording"]["events"] == 2
        assert state["telemetry"]["ignored_source"] == state["telemetry"]["rejected"] == 1
        assert client.get(f"/api/recordings/{started.json()['id']}/download").status_code == 404
        now[0] = 2.
        stopped = post(client, "/api/recordings/stop", {})
        assert stopped.status_code == 200 and stopped.json()["state"] == "complete"
        download = client.get(stopped.json()["download_url"])
        assert download.status_code == 200
        recording = read_recording(io.BytesIO(download.content))
        assert recording.started_at == 0. and recording.ended_at == 2.
        assert [event.frame for event in recording.events] == frames
        assert [event.received_at for event in recording.events] == [1., 1.]
        assert "attachment" in download.headers["content-disposition"]
        assert post(client, "/api/recordings/stop", {}).status_code == 409


@pytest.mark.parametrize("path", ["/api/sources", "/api/recordings/start", "/api/recordings/stop"])
@pytest.mark.parametrize("headers", [{}, {"origin": "https://foreign.example"},
                                     {"origin": "http://testserver.evil"}])
def test_mutations_refuse_missing_or_foreign_origin_before_side_effects(tmp_path, path, headers):
    session = ConsoleSession(ConsoleConfig(recordings_dir=tmp_path))
    app = create_app(session=session)
    with TestClient(app) as client:
        before = client.get("/api/state").json()
        assert client.post(path, json={}, headers=headers).status_code == 403
        after = client.get("/api/state").json()
        assert before["run_id"] == after["run_id"]
        assert after["configuration"] == before["configuration"]
        assert after["recording"]["state"] == "idle"
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("content,content_type,status", [
    (b"{}", "text/plain", 415),
    (b"{", "application/json", 422),
    (b" " * 8193, "application/json", 413),
    (b"[" * 1100 + b"]" * 1100, "application/json", 422),
])
def test_mutation_body_limits_and_parse_failures_return_controlled_errors(tmp_path, content,
                                                                        content_type, status):
    session = ConsoleSession(ConsoleConfig(recordings_dir=tmp_path))
    with TestClient(create_app(session=session), raise_server_exceptions=False) as client:
        response = client.post("/api/sources", content=content,
                               headers={**ORIGIN, "content-type": content_type})
        assert response.status_code == status
        assert client.get("/api/state").json()["run_id"] == session.run_id


@pytest.mark.parametrize("changes", [
    {"video_source": "file", "video_endpoint": "internet.mp4"},
    {"video_source": "device", "video_endpoint": "/dev/video0", "environment": "simulation"},
    {"mavlink_peer": "not-an-ip:14550"},
    {"system": 0}, {"recordings_dir": "/tmp/unrequested"},
])
def test_invalid_source_configuration_preserves_current_session(tmp_path, changes):
    with controlled_client(configured(tmp_path), real_udp=True) as (client, app, session, _, _now):
        values = {**session.config.public(), **changes}
        response = post(client, "/api/sources", values)
        assert response.status_code == 422
        assert app.state.session is session and not session._closed
        assert client.get("/api/state").json()["run_id"] == session.run_id
        assert not session.link.report(session.clock()).closed


def test_active_journal_blocks_source_replacement(tmp_path):
    with controlled_client(configured(tmp_path), real_udp=True) as (client, app, session, _, _now):
        assert post(client, "/api/recordings/start", {}).status_code == 200
        response = post(client, "/api/sources", {**session.config.public(), "component": 2})
        assert response.status_code == 409
        assert app.state.session is session and not session._closed
        assert session.recorder.active
        assert post(client, "/api/recordings/stop", {}).status_code == 200


def test_source_change_starts_new_run_and_preserves_last_completed_download(tmp_path):
    with controlled_client(configured(tmp_path), real_udp=True) as (client, app, session, _, now):
        assert post(client, "/api/recordings/start", {}).status_code == 200
        now[0] = 1.
        completed = post(client, "/api/recordings/stop", {}).json()
        original_bytes = client.get(completed["download_url"]).content
        response = post(client, "/api/sources", {**session.config.public(), "component": 2})
        assert response.status_code == 200
        state = response.json()
        assert state["run_id"] != session.run_id and state["telemetry"]["component"] == 2
        assert session._closed and app.state.session is not session
        assert app.state.session.recorder is session.recorder
        assert state["recording"]["state"] == "complete"
        assert state["recording"]["download_url"] == completed["download_url"]
        after = client.get(completed["download_url"])
        assert after.status_code == 200 and after.content == original_bytes
        journal = read_recording(io.BytesIO(after.content))
        assert journal.started_at == 0. and journal.ended_at == 1.


def test_link_error_preserves_valid_interrupted_journal_and_requires_link_recovery(tmp_path):
    with controlled_client(configured(tmp_path)) as (client, app, session, source, now):
        started = post(client, "/api/recordings/start", {}).json()
        source.chunks.append(frame(mav.MAVLink_heartbeat_message(2, 3, 1, 0, 4, 3)))
        now[0] = 1.
        session.tick()
        source.chunks.append(OSError("test input disconnected"))
        now[0] = 2.
        session.tick()
        state = client.get("/api/state").json()
        assert state["telemetry"]["state"] == "error"
        journal = state["recording"]
        assert journal["state"] == "complete" and journal["events"] == 1
        assert journal["ended_at"] == 2. and journal["end_reason"] == "transport_error"
        assert "test input disconnected" in journal["end_detail"]
        downloaded = client.get(journal["download_url"])
        assert downloaded.status_code == 200
        recorded = read_recording(io.BytesIO(downloaded.content))
        assert len(recorded.events) == 1 and recorded.end_reason == "transport_error"
        assert recorded.end_detail == journal["end_detail"]
        meta = client.get(f"/api/recordings/{started['id']}").json()
        assert meta["integrity"] == "verified" and meta["end_reason"] == "transport_error"
        assert post(client, "/api/recordings/stop", {}).status_code == 409
        assert post(client, "/api/recordings/start", {}).status_code == 409
        path = tmp_path / f"{started['id']}.jsonl"
        assert path.read_bytes() == downloaded.content


def test_start_requires_open_configured_link_and_exact_empty_object(tmp_path):
    session = ConsoleSession(ConsoleConfig(recordings_dir=tmp_path))
    with TestClient(create_app(session=session)) as client:
        assert post(client, "/api/recordings/start", {}).status_code == 409
        assert post(client, "/api/recordings/start", {"path": "unexpected"}).status_code == 422
        assert post(client, "/api/recordings/stop", []).status_code == 422
        assert list(tmp_path.iterdir()) == []


def test_recording_creation_error_is_explicit_and_does_not_advertise_download(tmp_path):
    invalid_directory = tmp_path / "already-a-file"
    invalid_directory.write_text("keep this file")
    with controlled_client(configured(invalid_directory)) as (client, app, session, _, _now):
        response = post(client, "/api/recordings/start", {})
        assert response.status_code == 500
        assert response.json()["state"] == "error"
        assert response.json()["download_url"] is None
        assert not session.recorder.active
        assert invalid_directory.read_text() == "keep this file"


def test_flush_failure_after_footer_is_not_announced_as_a_completed_download(tmp_path):
    recorder = ConsoleRecorder(tmp_path)
    started = recorder.start(0.)
    stream = recorder._stream

    class FlushFailure:
        def flush(self):
            raise OSError("test disk flush failed")

        def close(self):
            stream.close()

    recorder._stream = FlushFailure()
    stopped = recorder.stop(1.)
    assert stopped["state"] == "error" and stopped["download_url"] is None
    assert "test disk flush failed" in stopped["error"]
    assert recorder.completed_path(started["id"]) is None
    assert stream.closed


def test_session_shutdown_finishes_its_active_empty_journal(tmp_path):
    recorder = ConsoleRecorder(tmp_path)
    session = ConsoleSession(ConsoleConfig(recordings_dir=tmp_path), clock=lambda: 3.,
                             recorder=recorder)
    session.start()
    started = recorder.start(1.)
    session.close()
    assert recorder.snapshot()["state"] == "complete"
    with recorder.completed_path(started["id"]).open("rb") as stream:
        journal = read_recording(stream)
    assert journal.started_at == 1. and journal.ended_at == 3. and journal.events == ()
    assert journal.end_reason == "shutdown"


def test_received_message_total_does_not_expire_with_the_link_window(tmp_path):
    with controlled_client(configured(tmp_path)) as (client, app, session, source, now):
        source.chunks.append(frame(mav.MAVLink_heartbeat_message(2, 3, 1, 0, 4, 3)))
        now[0] = 1.
        session.tick()
        assert client.get("/api/state").json()["telemetry"]["rx_messages"] == 1
        now[0] = 5.
        session.tick()
        assert session.link.report(5.).traffic.rx == 0
        assert client.get("/api/state").json()["telemetry"]["rx_messages"] == 1
