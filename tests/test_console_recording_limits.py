"""The console never advertises an unreadable journal after capacity or link end."""
import io

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")

from fastapi.testclient import TestClient
from argos.backends.mavlink import Received, read_recording
from argos.console.app import create_app
from argos.console.archive import RecordingArchive
from argos.console.config import ConsoleConfig
from argos.console.recording import ConsoleRecorder
from argos.console.recording_limits import MAX_EVENTS, MAX_BYTES
from test_console_controls import configured, controlled_client, post, frame


def reception(at=0., *, large=False):
    message = (mav.MAVLink_v2_extension_message(0, 1, 1, 1024, [255] * 249) if large
               else mav.MAVLink_heartbeat_message(2, 3, 1, 0, 4, 3))
    raw = frame(message)
    return Received(at, 1, 1, 0, message.get_msgId(), message.get_type(), message.to_dict(), raw)


def test_default_event_limit_closes_a_journal_its_own_archive_can_download(tmp_path):
    recorder = ConsoleRecorder(tmp_path)
    started = recorder.start(0.)
    event = reception()
    for _ in range(MAX_EVENTS + 1):
        recorder.append(event)
    state = recorder.snapshot()
    assert state["state"] == "complete" and state["events"] == MAX_EVENTS == 100_000
    assert state["end_reason"] == "event_limit" and state["download_url"]
    assert state["size_bytes"] <= MAX_BYTES
    archive = RecordingArchive(tmp_path)
    meta = archive.metadata(started["id"])
    assert meta["integrity"] == "verified" and meta["events"] == MAX_EVENTS
    assert meta["end_reason"] == "event_limit"
    assert len(archive.download(started["id"])) == state["size_bytes"]


def test_size_limit_reserves_footer_and_context_and_preserves_all_accepted_frames(tmp_path):
    recorder = ConsoleRecorder(tmp_path, max_bytes=20_000)
    state = recorder.start(0., context={"note": "é" * 1500})
    event = reception(1., large=True)
    for _ in range(100):
        recorder.append(event)
        if not recorder.active:
            break
    closed = recorder.snapshot()
    assert closed["state"] == "complete" and closed["end_reason"] == "size_limit"
    assert 1 <= closed["events"] < 100
    archive = RecordingArchive(tmp_path)
    data = archive.download(state["id"])
    assert len(data) == closed["size_bytes"] <= 20_000
    journal = read_recording(io.BytesIO(data))
    assert len(journal.events) == closed["events"]
    assert all(item.frame == event.frame for item in journal.events)
    assert journal.end_reason == "size_limit" and journal.context["note"] == "é" * 1500


def test_auto_stop_leaves_live_acquisition_running_and_a_new_capture_can_start(tmp_path):
    with controlled_client(configured(tmp_path)) as (client, app, session, source, now):
        session.recorder.max_events = 2
        first = post(client, "/api/recordings/start", {}).json()
        raw = frame(mav.MAVLink_heartbeat_message(2, 3, 1, 0, 4, 3))
        source.chunks.append(raw * 3)
        now[0] = 1.
        session.tick()
        state = client.get("/api/state").json()
        assert state["telemetry"]["rx_messages"] == 3
        assert state["recording"]["events"] == 2 and state["recording"]["end_reason"] == "event_limit"
        assert not source.closed and not session.recorder.active
        first_bytes = client.get(state["recording"]["download_url"]).content
        second = post(client, "/api/recordings/start", {}).json()
        assert second["id"] != first["id"] and second["state"] == "recording"
        assert client.get(f"/api/recordings/{first['id']}/download").content == first_bytes
        source.chunks.append(raw)
        now[0] = 2.
        session.tick()
        second_closed = post(client, "/api/recordings/stop", {}).json()
        assert second_closed["events"] == 1 and second_closed["end_reason"] == "stopped"


def test_interrupted_closure_metadata_survives_console_restart(tmp_path):
    recorder = ConsoleRecorder(tmp_path)
    state = recorder.start(0.)
    recorder.append(reception(1.))
    detail = "Erreur transport : " + "🛰é" * 200
    closed = recorder.stop(2., reason="transport_error", detail=detail)
    assert closed["state"] == "complete" and closed["end_detail"].endswith("…")
    with TestClient(create_app(ConsoleConfig(recordings_dir=tmp_path))) as client:
        meta = client.get(f"/api/recordings/{state['id']}").json()
        assert meta["integrity"] == "verified" and meta["end_reason"] == "transport_error"
        assert meta["end_detail"] == closed["end_detail"]
        replayed = client.get(f"/api/recordings/{state['id']}/replay", params={
            "revision": meta["revision"], "at": 2., "system": 1, "component": 1})
        assert replayed.status_code == 200 and replayed.json()["received"] == 1
        assert client.get(meta["download_url"]).status_code == 200


def test_disk_failure_during_transport_closure_is_not_advertised_as_valid(tmp_path):
    with controlled_client(configured(tmp_path)) as (client, app, session, source, now):
        started = post(client, "/api/recordings/start", {}).json()
        stream = session.recorder._writer._stream

        class BrokenDisk:
            def write(self, _):
                raise OSError("disk unavailable")

        session.recorder._writer._stream = BrokenDisk()
        source.chunks.append(OSError("connection interrupted"))
        now[0] = 1.
        session.tick()
        state = client.get("/api/state").json()["recording"]
        assert state["state"] == "error" and state["download_url"] is None
        assert "disk unavailable" in state["error"] and stream.closed
        assert client.get(f"/api/recordings/{started['id']}/download").status_code == 404


def test_transport_error_with_undecodable_path_does_not_break_state_or_archive(tmp_path):
    with controlled_client(configured(tmp_path)) as (client, app, session, source, now):
        started = post(client, "/api/recordings/start", {}).json()
        source.chunks.append(OSError("path unavailable: /dev/bad\udcff"))
        now[0] = 1.
        session.tick()
        response = client.get("/api/state")
        assert response.status_code == 200
        state = response.json()
        assert "\\udcff" in state["telemetry"]["detail"]
        assert state["recording"]["state"] == "complete"
        assert client.get("/api/mavlink/messages").status_code == 200
        meta_response = client.get(f"/api/recordings/{started['id']}")
        assert meta_response.status_code == 200
        meta = meta_response.json()
        assert "\\udcff" in meta["end_detail"] and meta["end_reason"] == "transport_error"
        assert client.get(meta["download_url"]).status_code == 200
