"""Physical-camera preview is image-only and never acquires flight authority."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from argos.backends.mavlink import MavlinkLink, SequenceScope
from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.session import ConsoleSession
from argos.console.vision import AnalyzedFrame, VisionService
from test_console import Input

ORIGIN = {"origin": "http://testserver"}
PATH = "/api/vision/yaw-preview"


@pytest.fixture
def preview(tmp_path):
    now = [0.1]
    config = ConsoleConfig(environment="real", video_source="device",
        video_endpoint="/dev/video2", vision_model=Path("model.onnx"), recordings_dir=tmp_path)
    session = ConsoleSession(config, clock=lambda: now[0])
    session._started = True  # No physical camera or detector process in this fixture.
    vision = VisionService(None)
    vision._context = session.run_id, session.video_source_id
    client = TestClient(create_app(session=session, vision=vision))

    def observe(*, at=None, detections=None, width=2):
        at = now[0] if at is None else at
        ds = ([{"track_id": 1, "box": [.6, .2, .2, .5], "confidence": .9}]
              if detections is None else detections)
        session.video.accept_raw(width=width, height=2, step=width*3,
            pixel_format="RGB_INT8", data=bytes(width*6), received_at=at)
        sample = session.video.latest(at)
        candidate = AnalyzedFrame(sample, (session.run_id, session.video_source_id),
            {"width": width, "height": 2, "inference_ms": 1., "detections": ds})
        vision._frame = candidate
        vision._selection_history.append((candidate.context, sample.sequence, at,
                                          frozenset(d["track_id"] for d in ds)))
        session._observe_vision()
        return sample.sequence

    def body():
        return dict(action="select", revision=session.yaw_preview.state(now[0])["revision"],
            run_id=session.run_id, video_id=session.video_source_id,
            frame_sequence=vision._frame.sample.sequence, track_id=1)

    def select(**changes):
        return client.post(PATH, json=dict(body(), **changes), headers=ORIGIN)

    observe()
    yield locals()
    session.close()
    client.close()


def test_preview_works_without_telemetry_and_never_takes_control(preview):
    f = preview
    before = f["session"].control.state(f["now"][0])
    assert f["session"].link is None
    result = f["select"]()
    assert result.status_code == 200
    state = result.json()["yaw_preview"]
    assert state["phase"] == "tracking" and state["target_id"] == 1
    assert state["error_x"] == pytest.approx(.4)
    assert state["yaw"] == pytest.approx(.1)
    assert f["session"].control.state(f["now"][0]) == before
    assert f["client"].get("/api/state").json()["yaw_preview"] == state


def test_preview_is_passive_even_with_real_mavlink_receiver(preview):
    f = preview
    source = Input()  # write() raises AssertionError on any MAVLink emission.
    session = f["session"]
    session.link = MavlinkLink(source, sequence_scope=SequenceScope.COMPONENT)
    session.config = replace(session.config, mavlink_bind=("127.0.0.1", 0),
        mavlink_peer=("127.0.0.1", 14550), sequence_scope=SequenceScope.COMPONENT)
    assert f["select"]().status_code == 200
    session.tick()
    reads = source.reads
    f["client"].get("/api/state")
    assert source.reads == reads
    f["now"][0] = .6
    session.tick()
    assert session.state()["yaw_preview"]["yaw"] == 0
    session.close()
    assert source.closed


def test_expiry_on_http_read_is_latched_without_refreshing_vision(preview):
    f = preview
    assert f["select"]().status_code == 200
    original = f["vision"]._frame
    f["now"][0] = .551
    state = f["client"].get("/api/state").json()["yaw_preview"]
    assert state["phase"] == "stopped" and state["yaw"] == 0
    assert f["vision"]._frame is original
    f["observe"]()
    state = f["session"].state()["yaw_preview"]
    assert state["phase"] == "stopped" and state["target_id"] is None
    assert f["select"]().status_code == 200


def test_clear_fences_late_selection_and_is_independent_of_image_availability(preview):
    f = preview
    queued = f["body"]()
    f["vision"]._frame = None
    response = f["client"].post(PATH, json={"action": "clear",
        "run_id": f["session"].run_id, "video_id": f["session"].video_source_id}, headers=ORIGIN)
    assert response.status_code == 200 and response.json()["yaw_preview"]["yaw"] == 0
    f["observe"]()
    queued["frame_sequence"] = f["vision"]._frame.sample.sequence
    assert f["client"].post(PATH, json=queued, headers=ORIGIN).status_code == 409
    assert f["session"].state()["yaw_preview"]["target_id"] is None


@pytest.mark.parametrize("changes", [
    {"run_id": "old"}, {"video_id": "old"}, {"frame_sequence": 9876}, {"track_id": 12},
])
def test_selection_must_match_server_owned_displayed_history(preview, changes):
    assert preview["select"](**changes).status_code == 409
    assert preview["session"].state()["yaw_preview"]["yaw"] == 0


@pytest.mark.parametrize("changes", [
    {"revision": True}, {"frame_sequence": -1}, {"track_id": 0}, {"track_id": 2**53},
    {"box": [.1, .1, .1, .1]}, {"yaw": .1}, {"received_at": .1}, {"action": []},
    {"run_id": []}, {"video_id": ""},
])
def test_selection_rejects_extra_fields_and_invalid_types(preview, changes):
    assert preview["select"](**changes).status_code == 422


@pytest.mark.parametrize("body", [None, [], {}, {"action": "engage"}])
def test_invalid_request_does_not_change_state(preview, body):
    f = preview
    before = f["session"].state()["yaw_preview"]
    response = f["client"].post(PATH, content=json.dumps(body),
        headers={**ORIGIN, "content-type": "application/json"})
    assert response.status_code == 422
    assert f["session"].state()["yaw_preview"] == before


def test_selection_requires_same_origin_json(preview):
    f = preview
    assert f["client"].post(PATH, json=f["body"]()).status_code == 403
    assert f["client"].post(PATH, json=f["body"](), headers={"origin": "http://other"}).status_code == 403
    assert f["client"].post(PATH, content="{}", headers=ORIGIN).status_code == 415
    assert f["client"].post(PATH, content="x"*8193,
        headers={**ORIGIN, "content-type": "application/json"}).status_code == 413


@pytest.mark.parametrize("change", ["missing", "error", "lost", "dimension", "context"])
def test_selected_preview_clears_when_vision_changes(preview, change):
    f = preview
    assert f["select"]().status_code == 200
    f["now"][0] += .1
    if change == "missing":
        f["vision"]._frame = None
    elif change == "error":
        f["vision"]._error = "worker failed"
    elif change == "context":
        f["session"].video_source_id = "new-camera"
    else:
        f["observe"](detections=[] if change == "lost" else None,
                        width=3 if change == "dimension" else 2)
    f["session"].tick()
    state = f["session"].state()["yaw_preview"]
    assert state["phase"] == "stopped" and state["yaw"] == 0 and state["target_id"] is None


def test_camera_reconnect_and_source_replacement_drop_preview(preview, monkeypatch):
    import argos.console.session as session_module
    f = preview
    class Camera:
        worker_stopped = True
        def __init__(self, store):
            self.store = store
        def start(self):
            pass
        def close(self):
            self.store.stop()
    monkeypatch.setattr(session_module, "DeviceCamera", Camera)
    assert f["select"]().status_code == 200
    old = f["body"]()
    response = f["client"].post("/api/sources/video/reconnect", json={}, headers=ORIGIN)
    assert response.status_code == 200
    assert response.json()["yaw_preview"]["yaw"] == 0
    assert f["client"].post(PATH, json=old, headers=ORIGIN).status_code == 409
    f["vision"]._context = f["session"].run_id, f["session"].video_source_id
    f["observe"]()
    assert f["select"]().status_code == 200
    old = f["body"]()
    settings = f["session"].config.public()
    settings["video_endpoint"] = "/dev/video4"
    response = f["client"].post("/api/sources", json=settings, headers=ORIGIN)
    assert response.status_code == 200
    state = response.json()
    assert state["run_id"] != old["run_id"]
    assert state["yaw_preview"]["target_id"] is None and state["yaw_preview"]["yaw"] == 0
    assert f["client"].post(PATH, json=old, headers=ORIGIN).status_code == 409
    f["client"].app.state.session.close()


@pytest.mark.parametrize("config", [ConsoleConfig(),
    ConsoleConfig(environment="real", video_source="device", video_endpoint="/dev/video0"),
    ConsoleConfig(environment="simulation", video_source="gazebo", video_endpoint="/camera",
                  vision_model=Path("model.onnx")),
])
def test_preview_available_only_for_real_camera_with_detector(config, tmp_path):
    session = ConsoleSession(replace(config, recordings_dir=tmp_path))
    session._started = True
    client = TestClient(create_app(session=session))
    try:
        state = client.get("/api/state").json()["yaw_preview"]
        assert state["enabled"] is False and state["yaw"] == 0
        assert client.post(PATH, json={"action": "clear", "run_id": session.run_id,
            "video_id": session.video_source_id}, headers=ORIGIN).status_code == 409
    finally:
        session.close()
        client.close()
