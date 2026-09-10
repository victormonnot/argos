"""Visual capture and archive HTTP integration without any acquisition/control IO."""
import io
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("pymavlink")
pytest.importorskip("PIL")
from fastapi.testclient import TestClient

from argos.backends.mavlink import MavlinkLink, SequenceScope, read_recording
from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.context import capture_context
from argos.console.session import ConsoleSession
from argos.console.video import DeviceCamera, GazeboCamera
from argos.console.vision import AnalyzedFrame
from argos.console.visual_recording import MAX_SAMPLES, VisualArchive
from test_console_archive import journal, heartbeat
from test_console_controls import MemoryInput, frame as wire_frame


ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def camera_only(tmp_path, monkeypatch):
    now = [10.]
    config = ConsoleConfig(video_source="device", video_endpoint="/dev/video99",
                           environment="real", recordings_dir=tmp_path)
    session = ConsoleSession(config, clock=lambda: now[0])
    session._started = True
    def forbidden(*args, **kwargs):
        pytest.fail("Visual recording/replay must not open a source or emit flight commands")
    monkeypatch.setattr(DeviceCamera, "start", forbidden)
    monkeypatch.setattr(GazeboCamera, "start", forbidden)
    monkeypatch.setattr(session, "_link_factory", forbidden)
    monkeypatch.setattr(session.control, "_send", forbidden)
    # Deliberately no context manager: tests never start the app's real sources.
    client = TestClient(create_app(session=session))

    def frame(at, color=(25, 120, 70)):
        now[0] = at
        session.video.accept_raw(width=8, height=6, step=24, pixel_format="RGB_INT8",
                                 data=bytes(color) * 48, received_at=at)
        return session.video.latest(at)

    frame(9.95)
    now[0] = 10.
    result = SimpleNamespace(client=client, session=session, now=now, frame=frame,
                             directory=tmp_path)
    yield result
    if session.recorder.active:
        session.recorder.stop(now[0])
    session.recorder.visual.wait(3.)
    client.close()


def start(f):
    response = f.client.post("/api/recordings/start", json={"include_visual": True}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "recording"
    return response.json()["id"]


def stop(f, identifier, at=10.8):
    f.now[0] = at
    response = f.client.post("/api/recordings/stop", json={}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    # Waiting is test-only; the HTTP event-loop path must not join disk work.
    finished = f.session.recorder.visual.wait(3.)
    assert finished["state"] == "complete", finished
    response = f.client.get(f"/api/recordings/{identifier}")
    assert response.status_code == 200, response.text
    return response.json()


def replay(f, metadata, at):
    return f.client.get(f"/api/recordings/{metadata['id']}/visual", params={
        "revision": metadata["revision"], "visual_revision": metadata["visual"]["revision"], "at": at})


def complete_camera_recording(f):
    identifier = start(f)
    sample = f.frame(10.4, (150, 30, 20))
    f.session.tick()
    return stop(f, identifier), sample


def test_camera_only_start_stop_metadata_jpeg_and_replay_have_no_invented_telemetry(camera_only):
    f = camera_only
    first = f.session.video.latest(f.now[0])
    metadata, last = complete_camera_recording(f)
    assert metadata["sources"] == [] and metadata["events"] == 0
    assert metadata["context"]["telemetry_endpoint"] is None
    assert metadata["context"]["configuration"]["video_source"] == "device"
    assert metadata["visual"]["state"] == "complete" and metadata["visual"]["frames"] == 2
    assert metadata["duration_s"] == pytest.approx(.8)
    beginning = replay(f, metadata, 0).json()
    assert beginning["frame"]["at_s"] == pytest.approx(-.05)  # Actual pre-click receipt, not renewed.
    assert beginning["frame"]["available_at_s"] == 0
    assert f.client.get(beginning["frame"]["url"]).content == first.jpeg
    later_response = replay(f, metadata, .5)
    assert later_response.status_code == 200
    later = later_response.json()
    assert later["frame"]["sequence"] == last.sequence
    assert later["frame"]["detections"] == [] and later["frame"]["vision"] is None
    assert later["control"]["enabled"] is False and later["control"]["owned"] is False
    jpeg = f.client.get(later["frame"]["url"])
    assert jpeg.status_code == 200 and jpeg.headers["content-type"] == "image/jpeg"
    assert jpeg.content == last.jpeg
    downloaded = f.client.get(metadata["download_url"])
    recording = read_recording(io.BytesIO(downloaded.content))
    assert not recording.events
    # A camera-only journal must not manufacture MAVLink system/component 1.
    telemetry = f.client.get(f"/api/recordings/{metadata['id']}/replay", params={"revision": metadata["revision"]})
    assert telemetry.status_code == 422
    assert f.session.link is None and f.session.camera is None
    assert f.session.control.state(f.now[0])["owned"] is False


def test_legacy_empty_start_body_still_requires_telemetry(camera_only):
    f = camera_only
    response = f.client.post("/api/recordings/start", json={}, headers=ORIGIN)
    assert response.status_code == 409
    assert not f.session.recorder.active
    assert list(f.directory.iterdir()) == []


def test_raw_frame_does_not_gain_future_boxes_when_analysis_arrives_later(camera_only):
    f = camera_only
    sample = f.session.video.latest(f.now[0])
    identifier = start(f)
    f.now[0] = 10.2
    vision = f.session.vision
    vision._context = f.session.run_id, f.session.video_source_id
    vision._frame = AnalyzedFrame(sample, vision._context, {
        "width": 8, "height": 6, "inference_ms": 12.,
        "detections": [{"box": [.2, .1, .3, .7], "confidence": .8, "track_id": 7}]})
    f.session.tick()
    metadata = stop(f, identifier, 10.4)
    before = replay(f, metadata, .1).json()["frame"]
    after = replay(f, metadata, .3).json()["frame"]
    rewound = replay(f, metadata, .1).json()["frame"]
    assert before == rewound
    assert before["sequence"] == after["sequence"] == sample.sequence
    assert before["detections"] == [] and before["vision"] is None
    assert after["detections"][0]["track_id"] == 7
    assert after["available_at_s"] == pytest.approx(.2)
    assert before["index"] != after["index"]
    assert f.client.get(before["url"]).content == f.client.get(after["url"]).content == sample.jpeg


@pytest.mark.parametrize("wrong_key", ["revision", "visual_revision"])
def test_both_revisions_fence_replay_frame_and_media_download(camera_only, wrong_key):
    f = camera_only
    metadata, _ = complete_camera_recording(f)
    view = replay(f, metadata, .5).json()
    params = {"revision": metadata["revision"], "visual_revision": metadata["visual"]["revision"]}
    params[wrong_key] = "0" * 64
    base = f"/api/recordings/{metadata['id']}/visual"
    for path in (base, f"{base}/frames/{view['frame']['index']}.jpg", f"{base}/download"):
        response = f.client.get(path, params=params)
        assert response.status_code == 409, (path, response.text)
    assert replay(f, metadata, .5).status_code == 200
    assert f.client.get(metadata["download_url"]).status_code == 200


@pytest.mark.parametrize("index", [-1, MAX_SAMPLES, 2**64])
def test_invalid_visual_image_index_returns_http_validation_error(camera_only, index):
    f = camera_only
    metadata, _ = complete_camera_recording(f)
    response = f.client.get(f"/api/recordings/{metadata['id']}/visual/frames/{index}.jpg", params={
        "revision": metadata["revision"], "visual_revision": metadata["visual"]["revision"]})
    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid archived image index"


@pytest.mark.parametrize("operation,key", [("action", "action"), ("framing", "operation")])
@pytest.mark.parametrize("malformed", [[], {}])
def test_optional_capture_preserves_original_error_for_malformed_control_values(camera_only, operation, key, malformed):
    f = camera_only
    body = {"token": "not-authority", key: malformed}
    path = f"/api/control/{operation}"
    original = f.client.post(path, json=body, headers=ORIGIN)
    assert original.status_code == 409  # This camera-only session has no control transport.
    identifier = start(f)
    recorded = f.client.post(path, json=body, headers=ORIGIN)
    assert recorded.status_code == original.status_code and recorded.json() == original.json()
    metadata = stop(f, identifier)
    events = replay(f, metadata, .8).json()["events"]
    assert any(event["kind"] == operation and event["status"] == "refused" for event in events)


@pytest.mark.parametrize("failed_method", ["start", "stop"])
def test_optional_writer_lifecycle_exception_does_not_fail_the_journal(camera_only, monkeypatch, failed_method):
    f = camera_only
    visual = f.session.recorder.visual
    original_stop = visual.stop
    def fail(*args, **kwargs):
        raise OSError("test optional writer lifecycle failure")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(visual, failed_method, fail)
            identifier = start(f)
            # The normal no-transport refusal must remain intact even when
            # optional instrumentation could not initialize or finalize.
            refused = f.client.post("/api/control/action", json={"token": "absent", "action": "land"}, headers=ORIGIN)
            assert refused.status_code == 409
            assert refused.json()["detail"] == "An open simulation link is required"
            f.now[0] = 10.4
            response = f.client.post("/api/recordings/stop", json={}, headers=ORIGIN)
            assert response.status_code == 200
            status = response.json()
            assert status["state"] == "complete" and status["error"] == ""
            assert status["visual"]["state"] == "error"
            downloaded = f.client.get(status["download_url"])
            assert downloaded.status_code == 200
            journal_data = read_recording(io.BytesIO(downloaded.content))
            assert journal_data.ended_at == 10.4 and not journal_data.events
            assert not f.session._error and not f.session.control.state(10.4)["owned"]
            if failed_method == "stop":
                assert visual.wait(3.)["state"] == "error"
                assert not visual._thread.is_alive()
        # Restore the deliberately failed method before the new explicit run.
        restarted = start(f)
        assert restarted != identifier
        assert stop(f, restarted)["visual"]["state"] == "complete"
    finally:
        # A deliberately replaced stop() may have left a writer waiting. This
        # is test cleanup, not a claim that a failed producer finalized media.
        original_stop(f.now[0], reason="test_cleanup")
        visual.wait(3.)


@pytest.mark.parametrize("include_visual", [False, True])
def test_transport_error_keeps_only_visual_enabled_journal_open(camera_only, include_visual):
    f = camera_only
    source = MemoryInput()  # Its write() is a fail-fast emission guard.
    f.session.config = replace(f.session.config, mavlink_tcp=("127.0.0.1", 5860),
                               sequence_scope=SequenceScope.CHANNEL)
    f.session._endpoint = f.session.config.telemetry_endpoint
    f.session.link = MavlinkLink(source, sequence_scope=SequenceScope.CHANNEL)
    response = f.client.post("/api/recordings/start", json={"include_visual": True} if include_visual else {}, headers=ORIGIN)
    assert response.status_code == 200
    identifier = response.json()["id"]
    source.chunks.append(wire_frame(heartbeat()))
    f.now[0] = 10.2
    f.session.tick()
    source.chunks.append(OSError("test receiver disconnected"))
    f.now[0] = 10.4
    f.session.tick()
    assert f.session._error and source.closed
    assert f.session.recorder.active is include_visual
    if include_visual:
        latest = f.frame(10.6, (30, 40, 180))
        f.session.tick()
        metadata = stop(f, identifier)
        assert replay(f, metadata, .7).json()["frame"]["sequence"] == latest.sequence
    else:
        assert f.session.recorder.snapshot()["end_reason"] == "transport_error"
        metadata = f.client.get(f"/api/recordings/{identifier}").json()
        assert metadata["visual"]["state"] == "missing"
    assert metadata["events"] == 1
    assert metadata["sources"] == [{"system": 1, "component": 1, "events": 1}]
    assert f.client.get(metadata["download_url"]).status_code == 200


def test_invalid_media_is_isolated_from_its_valid_journal(camera_only):
    f = camera_only
    metadata, _ = complete_camera_recording(f)
    journal_bytes = f.client.get(metadata["download_url"]).content
    (f.directory / f"{metadata['id']}.visual.sqlite3").write_bytes(b"not a database!" * 600)
    response = f.client.get(f"/api/recordings/{metadata['id']}")
    assert response.status_code == 200
    invalid = response.json()
    assert invalid["integrity"] == "verified" and invalid["visual"]["state"] == "invalid"
    assert invalid["revision"] == metadata["revision"]
    assert replay(f, metadata, .5).status_code == 422
    assert f.client.get(metadata["download_url"]).content == journal_bytes
    assert any(item["id"] == metadata["id"] for item in f.client.get("/api/recordings").json()["items"])
    assert not f.session._error and not f.session.control.state(f.now[0])["owned"]


@pytest.mark.parametrize("change_at", ["after_validation", "during_read"])
def test_media_download_rejects_changes_after_validation_and_closes_verified_stream(camera_only, monkeypatch, change_at):
    f = camera_only
    metadata, _ = complete_camera_recording(f)
    path = f.directory / f"{metadata['id']}.visual.sqlite3"
    original = VisualArchive.open_download
    streams = []
    def alter_file():
        with path.open("ab") as changed:
            changed.write(b"changed-after-validation")
    class ChangeOnRead:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, key):
            return getattr(self.stream, key)
        def read(self, size):
            data = self.stream.read(size)
            alter_file()
            return data
    def opened(archive, *args, **kwargs):
        stream = original(archive, *args, **kwargs)
        streams.append(stream)
        if change_at == "after_validation":
            alter_file()
            return stream
        return ChangeOnRead(stream)
    monkeypatch.setattr(VisualArchive, "open_download", opened)
    with pytest.raises(RuntimeError, match="Visual recording changed during download"):
        f.client.get(f"/api/recordings/{metadata['id']}/visual/download", params={
            "revision": metadata["revision"], "visual_revision": metadata["visual"]["revision"]})
    assert streams and all(stream.closed for stream in streams)
    assert f.client.get(metadata["download_url"]).status_code == 200


def test_capture_provider_error_finishes_only_optional_media(camera_only, monkeypatch):
    f = camera_only
    identifier = start(f)
    def fail(session):
        raise RuntimeError("unavailable optional analysis provider")
    monkeypatch.setattr(f.session.vision, "frame", fail)
    f.frame(10.2)
    f.session.tick()
    visual = f.session.recorder.visual.wait(3.)
    assert visual["state"] == "complete" and visual["end_reason"] == "capture_error"
    assert f.session.recorder.active and not f.session._error
    metadata = stop(f, identifier, 10.4)
    assert metadata["integrity"] == "verified"
    assert metadata["visual"]["end_reason"] == "capture_error"
    assert f.client.get(metadata["download_url"]).status_code == 200
    assert replay(f, metadata, .3).json()["state"] == "ended"


@pytest.mark.parametrize("with_context", [False, True])
def test_old_journals_without_media_remain_downloadable_and_replayable(camera_only, with_context):
    f = camera_only
    identifier = "a" * 32
    config = ConsoleConfig(environment="simulation", mavlink_tcp=("127.0.0.1", 5860),
                           sequence_scope=SequenceScope.CHANNEL)
    context = capture_context(config, "b" * 32, config.telemetry_endpoint) if with_context else None
    original = journal(f.directory / f"{identifier}.jsonl", [(11., heartbeat(), 1)], context=context)
    response = f.client.get(f"/api/recordings/{identifier}")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["visual"]["state"] == "missing"
    assert metadata["sources"] == [{"system": 1, "component": 1, "events": 1}]
    assert f.client.get(metadata["download_url"]).content == original
    replayed = f.client.get(f"/api/recordings/{identifier}/replay", params={"revision": metadata["revision"], "at": 1.})
    assert replayed.status_code == 200 and replayed.json()["heartbeat"]["state"] == "recent"


def test_pilot_throttle_engage_event_names_requested_authority_even_when_refused(camera_only):
    f = camera_only
    identifier = start(f)
    response = f.client.post("/api/control/framing", headers=ORIGIN, json={
        "token": "not-authority", "operation": "engage", "profile": "pilot_throttle"})
    assert response.status_code == 409
    metadata = stop(f, identifier)
    events = replay(f, metadata, .8).json()["events"]
    requested = [event for event in events if event["kind"] == "framing"]
    assert len(requested) == 1
    assert requested[0]["status"] == "refused"
    assert requested[0]["detail"].startswith("Engage framing with manual throttle refused")
