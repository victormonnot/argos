"""Historical reception stays validated, bounded and separate from live state."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from threading import Event

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
from fastapi.testclient import TestClient

from argos.backends.mavlink import Received, RecordingWriter, read_recording
from argos.console import archive as module
from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.session import ConsoleSession

ID = "a" * 32
OTHER = "b" * 32


def heartbeat(mode=0):
    return mav.MAVLink_heartbeat_message(2, 3, 1, mode, 4, 3)


def attitude(roll=0., boot=1):
    return mav.MAVLink_attitude_message(boot, roll, 0., 0., 0., 0., 0.)


def journal(path, entries=(), *, start=10., end=15., wire_version=2, context=None):
    stream = io.BytesIO()
    writer = RecordingWriter(stream, started_at=start, context=context)
    for sequence, entry in enumerate(entries):
        at, message, component, *source = entry
        system = source[0] if source else 1
        encoder = mav.MAVLink(None, srcSystem=system, srcComponent=component)
        encoder.seq = sequence % 256
        raw = bytes(message.pack(encoder, force_mavlink1=wire_version == 1))
        decoded, = mav.MAVLink(None).parse_buffer(raw)
        writer.append(Received(at, system, component, encoder.seq, decoded.get_msgId(),
                               decoded.get_type(), decoded.to_dict(), raw))
    writer.finish(end)
    path.write_bytes(stream.getvalue())
    return stream.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(ConsoleConfig(recordings_dir=tmp_path))) as client:
        yield client


def replay(client, *, at=0., component=1, identifier=ID, revision=None):
    revision = revision or client.get(f"/api/recordings/{identifier}").json()["revision"]
    return client.get(f"/api/recordings/{identifier}/replay", params={
        "at": at, "system": 1, "component": component, "revision": revision})


def messages(client, *, identifier=ID, revision=None, **params):
    if revision is None:
        revision = client.get(f"/api/recordings/{identifier}").json()["revision"]
    return client.get(f"/api/recordings/{identifier}/messages", params={"revision": revision, **params})


def test_disk_journals_survive_restart_without_invented_provenance(client, tmp_path):
    data = journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1)])
    items = client.get("/api/recordings").json()["items"]
    assert items == [{"id": ID, "size_bytes": len(data), "modified_at":
                      (tmp_path / f"{ID}.jsonl").stat().st_mtime, "state": "unverified"}]
    meta = client.get(f"/api/recordings/{ID}").json()
    assert meta["duration_s"] == 5. and meta["events"] == 1
    assert meta["integrity"] == "verified" and meta["sources"] == [{"system": 1, "component": 1, "events": 1}]
    assert "environment" not in meta and "endpoint" not in meta
    assert client.get("/api/state").json()["recording"]["state"] == "idle"
    downloaded = client.get(meta["download_url"])
    assert downloaded.content == data and "attachment" in downloaded.headers["content-disposition"]
    assert read_recording(io.BytesIO(data)).started_at == 10.


def test_replay_uses_cursor_not_live_clock_and_supports_backward_seek(client, tmp_path):
    journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1), (11., attitude(), 1),
                                      (12., attitude(1., boot=0), 1)])
    before = replay(client).json()
    assert before["attitude"]["state"] == "absent" and before["received"] == 0
    assert before["next_at_s"] == 1.
    at = replay(client, at=1.).json()
    assert at["attitude"]["state"] == "recent" and at["attitude"]["fields"]["roll"] == 0.
    assert at["heartbeat"]["rx_age_s"] == 0.
    silence = replay(client, at=1.5).json()
    assert silence["attitude"]["state"] == "stale" and silence["heartbeat"]["state"] == "recent"
    end = replay(client, at=5.).json()
    assert end["attitude"]["fields"]["roll"] == 1. and end["attitude"]["state"] == "stale"
    assert end["attitude"]["boot_progress"] == "decreased" and end["previous_at_s"] == 2.
    assert end["next_at_s"] is None
    assert replay(client, at=1.).json() == at
    assert client.get("/api/state").json()["telemetry"]["attitude"]["fields"] is None


def test_replay_filters_sources_rejects_nan_and_never_borrows_future_diagnostics(client, tmp_path):
    journal(tmp_path / f"{ID}.jsonl", [(11., attitude(.5), 1), (12., attitude(2.), 42),
                                      (13., attitude(float("nan")), 1)])
    early = replay(client, at=1.).json()
    assert early["rejected"] == 0 and early["last_rejection"] == ""
    late = replay(client, at=3.).json()
    assert late["rejected"] == 1 and late["ignored_source"] == 1 and late["accepted"] == 1
    assert late["attitude"]["fields"]["roll"] == .5 and late["attitude"]["rx_age_s"] == 2.
    other = replay(client, at=2., component=42).json()
    assert other["attitude"]["fields"]["roll"] == 2. and other["accepted"] == 1
    assert replay(client, component=43).status_code == 422


def test_battery_sentinels_zero_and_ned_remain_distinct(client, tmp_path):
    battery = mav.MAVLink_sys_status_message(0, 0, 0, 0, 65535, 0, 0, 0, 0, 0, 0, 0, 0)
    position = mav.MAVLink_local_position_ned_message(1, 0., 0., -3., 0., 0., 0.)
    journal(tmp_path / f"{ID}.jsonl", [(11., battery, 1), (11., position, 1)])
    value = replay(client, at=1.).json()
    assert value["battery"]["voltage_v"] is None
    assert value["battery"]["current_a"] == 0. and value["battery"]["remaining_percent"] == 0
    assert value["local_position_ned"]["fields"]["z"] == -3.
    assert replay(client, at=3.).json()["battery"]["state"] == "recent"
    assert replay(client, at=3.01).json()["battery"]["state"] == "stale"


@pytest.mark.parametrize("damage", ["truncated", "checksum", "trailing", "nan_header"])
def test_invalid_files_expose_no_partial_replay_or_download(client, tmp_path, damage):
    path = tmp_path / f"{ID}.jsonl"
    data = journal(path, [(11., heartbeat(), 1)])
    if damage == "truncated":
        data = data.rsplit(b'\n', 2)[0] + b'\n'
    elif damage == "checksum":
        data = data.replace(b'"received_at":11.0', b'"received_at":12.0')
    elif damage == "trailing":
        data += b'{}\n'
    else:
        data = data.replace(b'"started_at":10.0', b'"started_at":NaN')
    path.write_bytes(data)
    assert client.get(f"/api/recordings/{ID}").status_code == 422
    assert client.get(f"/api/recordings/{ID}/download").status_code == 422
    assert replay(client, revision="old").status_code == 422
    assert messages(client, revision="old").status_code == 422


def test_replaced_valid_file_invalidates_cached_revision(client, tmp_path):
    path = tmp_path / f"{ID}.jsonl"
    journal(path, [(11., heartbeat(), 1)])
    meta = client.get(f"/api/recordings/{ID}").json()
    assert replay(client, at=1., revision=meta["revision"]).status_code == 200
    assert messages(client, revision=meta["revision"]).status_code == 200
    journal(path, [(11., heartbeat(4), 1)])
    assert replay(client, at=1., revision=meta["revision"]).status_code == 409
    assert client.get(meta["download_url"]).status_code == 409
    assert messages(client, revision=meta["revision"]).status_code == 409
    assert replay(client, at=1.).json()["mode"]["custom_mode"] == 4


def test_catalog_ignores_symlinks_special_files_and_arbitrary_names(client, tmp_path):
    journal(tmp_path / "unrelated.jsonl")
    (tmp_path / f"{ID}.jsonl").symlink_to(tmp_path / "unrelated.jsonl")
    os.mkfifo(tmp_path / f"{OTHER}.jsonl")
    assert client.get("/api/recordings").json()["items"] == []
    for identifier in (ID, OTHER, "unrelated", "%2e%2e%2fsecret"):
        assert client.get(f"/api/recordings/{identifier}/download").status_code == 404


def test_catalog_limit_and_large_file_are_explicit(client, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "LIST_LIMIT", 2)
    monkeypatch.setattr(module, "MAX_BYTES", 100)
    for i in range(3):
        path = tmp_path / f"{i:032x}.jsonl"
        journal(path)
        os.utime(path, (i + 1, i + 1))
    catalog = client.get("/api/recordings").json()
    assert catalog["total"] == 3 and catalog["limit"] == 2
    assert [item["id"] for item in catalog["items"]] == [f"{i:032x}" for i in (2, 1)]
    assert all(item["state"] == "too_large" for item in catalog["items"])
    assert client.get(f"/api/recordings/{2:032x}").status_code == 413


def test_event_limit_rejects_whole_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1), (12., heartbeat(), 1)])
    assert client.get(f"/api/recordings/{ID}").status_code == 422


@pytest.mark.parametrize("at", [-1, 6, "nan", "inf"])
def test_invalid_cursor_is_not_clamped_to_plausible_measurement(client, tmp_path, at):
    journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1)])
    assert replay(client, at=at).status_code == 422


def test_empty_and_zero_duration_recordings_can_be_inspected(client, tmp_path):
    journal(tmp_path / f"{ID}.jsonl", end=10.)
    metadata = client.get(f"/api/recordings/{ID}").json()
    assert metadata["duration_s"] == 0. and metadata["events"] == 0 and metadata["sources"] == []
    assert client.get(metadata["download_url"]).status_code == 200
    assert replay(client).status_code == 422
    assert messages(client).json()["items"] == []
    assert messages(client).json()["message_types"] == []
    assert messages(client).json()["total"] == 0


def test_active_capture_remains_separate_and_reader_does_not_block_receiver(tmp_path, monkeypatch):
    journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1)])
    session = ConsoleSession(ConsoleConfig(recordings_dir=tmp_path))
    entered, release = Event(), Event()
    original = module.read_recording

    def slow_reader(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "read_recording", slow_reader)
    with TestClient(create_app(session=session)) as client, ThreadPoolExecutor() as workers:
        started = session.recorder.start(session.clock())
        task = workers.submit(client.get, f"/api/recordings/{ID}")
        assert entered.wait(3)
        try:
            state = client.get("/api/state").json()
            assert state["recording"]["id"] == started["id"] and state["recording"]["state"] == "recording"
            assert client.get(f"/api/recordings/{started['id']}").status_code == 404
            items = client.get("/api/recordings").json()["items"]
            assert next(item for item in items if item["id"] == started["id"])["state"] == "recording"
        finally:
            release.set()
        assert task.result(timeout=5).status_code == 200


def test_message_pages_preserve_file_order_and_filter_exact_source_and_type(client, tmp_path):
    journal(tmp_path / f"{ID}.jsonl", [
        (11., heartbeat(), 1), (11., attitude(.5), 1), (11., attitude(1.), 42),
        (12., mav.MAVLink_statustext_message(6, b"payload retained"), 42),
        (13., attitude(2.), 1, 2), (13., heartbeat(), 0, 0), (14., attitude(3.), 1),
    ])
    page = messages(client, offset=1, limit=2).json()
    assert page["id"] == ID and len(page["revision"]) == 64
    assert page["total"] == 7 and page["offset"] == 1 and page["limit"] == 2
    assert [item["index"] for item in page["items"]] == [2, 3]
    assert [item["at_s"] for item in page["items"]] == [1., 1.]
    expected_types = [{"message_id": 0, "type_name": "HEARTBEAT", "count": 2},
                      {"message_id": 30, "type_name": "ATTITUDE", "count": 4},
                      {"message_id": 253, "type_name": "STATUSTEXT", "count": 1}]
    assert page["message_types"] == expected_types
    selected = messages(client, offset=1, limit=1, system=1, component=1, message_id=30).json()
    assert selected["total"] == 2 and selected["items"][0]["index"] == 7
    assert selected["message_types"] == expected_types
    by_type = messages(client, message_id=30).json()
    assert [item["index"] for item in by_type["items"]] == [2, 3, 5, 7]
    other_system = messages(client, system=2, component=1).json()
    assert other_system["total"] == 1 and other_system["items"][0]["index"] == 5
    zero_source = messages(client, system=0, component=0).json()
    assert zero_source["total"] == 1 and zero_source["items"][0]["index"] == 6
    text = messages(client, message_id=253).json()["items"][0]
    assert text["fields"]["text"] == "payload retained" and text["component"] == 42
    assert messages(client, offset=100).json()["items"] == []
    assert messages(client, message_id=16777215).json()["total"] == 0
    assert messages(client, system=255, component=255).json()["items"] == []


@pytest.mark.parametrize("wire_version", [1, 2])
def test_message_wire_bytes_and_receive_metadata_are_exact(client, tmp_path, wire_version):
    data = journal(tmp_path / f"{ID}.jsonl", [(11.25, heartbeat(4), 42), (12.5, attitude(.5), 1)],
                   wire_version=wire_version)
    recording = read_recording(io.BytesIO(data))
    items = messages(client).json()["items"]
    for item, event in zip(items, recording.events, strict=True):
        assert bytes.fromhex(item["frame_hex"]) == event.frame
        assert item["frame_bytes"] == len(event.frame) and item["wire_version"] == wire_version
        assert item["received_at"] == event.received_at
        assert item["at_s"] == event.received_at - recording.started_at
        assert (item["system"], item["component"], item["sequence"], item["message_id"], item["type_name"]) == (
            event.system, event.component, event.sequence, event.message_id, event.type_name)
        assert item["fields"] == dict(event.fields)


def test_message_fields_keep_nonfinite_values_explicit_and_json_safe(client, tmp_path):
    values = [float("nan"), float("inf"), -float("inf")]
    payload = mav.MAVLink_attitude_message(1, *values, 0., 0., 0.)
    covariance = mav.MAVLink_vision_position_estimate_message(1, 0., 0., 0., 0., 0., 0.,
                                                           covariance=values + [0.] * 18)
    data = journal(tmp_path / f"{ID}.jsonl", [(11., payload, 1), (12., covariance, 1)])
    response = messages(client)
    assert response.status_code == 200
    # A strict decoder must accept the result; no bare NaN / Infinity JSON tokens.
    page = json.loads(response.content, parse_constant=lambda value: pytest.fail(f"Non-JSON number: {value}"))
    fields = page["items"][0]["fields"]
    assert [fields[key] for key in ("roll", "pitch", "yaw")] == ["NaN", "Infinity", "-Infinity"]
    assert page["items"][1]["fields"]["covariance"] == ["NaN", "Infinity", "-Infinity"] + [0.] * 18
    assert [item["frame_hex"] for item in page["items"]] == [
        event.frame.hex() for event in read_recording(io.BytesIO(data)).events]
    assert replay(client, at=2.).json()["attitude"]["state"] == "absent"


def test_message_uint64_payload_does_not_lose_precision_in_browser(client, tmp_path):
    journal(tmp_path / f"{ID}.jsonl", [(11., mav.MAVLink_system_time_message(2**64 - 1, 42), 1)])
    fields = messages(client).json()["items"][0]["fields"]
    assert fields["time_unix_usec"] == "18446744073709551615"
    assert fields["time_boot_ms"] == 42


@pytest.mark.parametrize("params", [
    {"offset": -1}, {"offset": "nan"}, {"limit": 0}, {"limit": 101}, {"limit": "invalid"},
    {"system": 1}, {"component": 1}, {"system": -1, "component": 1},
    {"system": 1, "component": 256}, {"system": 256, "component": 1},
    {"system": 1, "component": -1}, {"system": "abc", "component": 1},
    {"message_id": -1}, {"message_id": 16777216}, {"message_id": "nan"},
])
def test_message_arguments_are_bounded_and_source_pair_is_required(client, tmp_path, params):
    journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1)])
    assert messages(client, **params).status_code == 422


def test_message_revision_required_and_maximum_page_is_enforced(client, tmp_path):
    journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1)] * 120)
    assert client.get(f"/api/recordings/{ID}/messages").status_code == 422
    assert len(messages(client).json()["items"]) == 50
    assert len(messages(client, limit=100).json()["items"]) == 100
    assert len(messages(client, offset=100, limit=100).json()["items"]) == 20


def test_active_and_failed_capture_messages_are_unavailable(client, monkeypatch):
    recorder = client.app.state.session.recorder
    identifier = recorder.start(client.app.state.session.clock())["id"]

    def unexpected_read(*args):
        pytest.fail("An active or failed journal must not enter the archive reader")

    monkeypatch.setattr(module.RecordingArchive, "messages", unexpected_read)
    assert messages(client, identifier=identifier, revision="unused").status_code == 404
    recorder.fail("receiver stopped")
    assert messages(client, identifier=identifier, revision="unused").status_code == 404


def test_message_validation_runs_outside_live_receiver_loop(client, tmp_path, monkeypatch):
    import hashlib

    data = journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1)])
    revision = hashlib.sha256(data).hexdigest()
    entered, release = Event(), Event()
    original = module.read_recording

    def slow_reader(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "read_recording", slow_reader)
    with ThreadPoolExecutor() as workers:
        task = workers.submit(messages, client, revision=revision)
        assert entered.wait(3)
        try:
            state = client.get("/api/state")
            assert state.status_code == 200 and state.json()["recording"]["state"] == "idle"
        finally:
            release.set()
        assert task.result(timeout=5).json()["items"][0]["type_name"] == "HEARTBEAT"
