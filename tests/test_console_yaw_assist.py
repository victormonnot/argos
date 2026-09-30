"""The local radio-selection transaction uses current server-owned imagery."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from argos.backends import yaw_stream_source
from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.session import ConsoleSession
from argos.console.vision import AnalyzedFrame, VisionService


STATE = "/api/vision/yaw-assist/state"
SELECT = "/api/vision/yaw-assist/select-center"
ORIGIN = {"origin": "http://127.0.0.1:8080"}


def person(identity=7, center=.7):
    return {"track_id": identity, "box": [center - .05, .25, .1, .5], "confidence": .9}


@pytest.fixture
def fly(tmp_path):
    now = [1.]
    config = ConsoleConfig(environment="real", video_source="device", video_endpoint="/dev/video2",
                           vision_model=Path("model.onnx"), yaw_assist=True, recordings_dir=tmp_path)
    session = ConsoleSession(config, clock=lambda: now[0])
    session._started = True  # Drive imagery directly; no serial/camera/worker starts.
    vision = VisionService(Path("model.onnx"))
    vision._ready = True
    vision._context = session.run_id, session.video_source_id
    client = TestClient(create_app(session=session, vision=vision), base_url="http://127.0.0.1:8080")

    def observe(at, detections=None):
        now[0] = at
        session.video.accept_raw(width=2, height=2, step=6, pixel_format="RGB_INT8",
                                 data=bytes(12), received_at=at)
        sample = session.video.latest(at)
        detections = [person()] if detections is None else detections
        vision._frame = AnalyzedFrame(sample, vision._context,
            {"width": 2, "height": 2, "inference_ms": 30., "detections": detections},
            appearances=tuple(tuple([1.] + [0.] * 207) for _ in detections))
        session._observe_vision()

    def body(generation=1):
        return {"request_id": f"1234abcd:{generation}", "run_id": session.run_id,
                "video_id": session.video_source_id}

    def select(generation=1, **changes):
        return client.post(SELECT, json={**body(generation), **changes}, headers=ORIGIN)

    observe(1.)
    yield locals()
    session.close()
    client.close()


def test_compact_state_is_accepted_by_real_source_and_never_carries_descriptors(fly):
    f = fly
    response = f["select"]()
    assert response.status_code == 200
    state = f["client"].get(STATE).json()
    demand = yaw_stream_source.YawValidator().validate(state, 100., 100.01)
    assert demand.valid and demand.value == 102
    assert set(state) == {"schema_version", "run_id", "at", "environment", "configuration",
                          "video", "vision", "yaw_preview", "reconnecting"}
    assert not {"telemetry", "events", "recordings", "appearance", "appearances"} & set(state)
    assert "appearances" not in json.dumps(state)
    header = f["client"].get("/api/vision/frame.jpg").headers["x-vision-result"]
    assert "appearance" not in header
    assert f["vision"]._frame.appearances is not None
    assert f["session"].link is None


def test_selection_success_is_idempotent_and_duplicate_cannot_select_after_clear(fly):
    f = fly
    first = f["select"](0)  # The radio's 31-bit generation can wrap to zero.
    assert first.status_code == 200
    original = first.json()["state"]["yaw_preview"]
    repeated = f["select"](0).json()["state"]["yaw_preview"]
    assert repeated == original
    f["session"].yaw_preview.clear()
    f["observe"](1.1, [person(9, .5)])
    duplicate = f["select"](0).json()["state"]["yaw_preview"]
    assert duplicate["target_id"] is None
    assert f["select"](1).json()["state"]["yaw_preview"]["target_id"] == 9


def test_failed_selection_does_not_arm_a_pending_future_target(fly):
    f = fly
    f["observe"](1.1, [])
    assert f["select"]().status_code == 409
    f["observe"](1.2)
    assert f["select"]().status_code == 409
    assert f["client"].get(STATE).json()["yaw_preview"]["target_id"] is None
    assert f["select"](2).status_code == 200


def test_explicit_radio_reenable_keeps_a_valid_target_when_another_is_nearer_center(fly):
    f = fly
    first = f["select"]().json()["state"]["yaw_preview"]
    f["observe"](1.1, [person(), person(9, .5)])
    again = f["select"](2).json()["state"]["yaw_preview"]
    assert again["target_id"] == 7
    assert again["selection_epoch"] == first["selection_epoch"]


@pytest.mark.parametrize("field", ["run_id", "video_id"])
def test_radio_selection_requires_observed_source_context_before_any_change(fly, field):
    f = fly
    before = f["session"].yaw_preview.selection_epoch
    assert f["select"](**{field: "old-context"}).status_code == 409
    assert f["session"].yaw_preview.selection_epoch == before


def test_radio_selection_requires_enabled_config_same_origin_and_bounded_json(fly):
    f = fly
    assert f["client"].post(SELECT, json=f["body"]()).status_code == 403
    assert f["client"].post(SELECT, json=f["body"](), headers={"origin": "http://elsewhere"}).status_code == 403
    assert f["client"].post(SELECT, content="{}", headers=ORIGIN).status_code == 415
    assert f["client"].post(SELECT, content="x" * 8193,
        headers={**ORIGIN, "content-type": "application/json"}).status_code == 413
    assert f["select"](request_id="1234abcd:2147483648").status_code == 422
    f["session"].config = replace(f["session"].config, yaw_assist=False)
    assert f["select"]().status_code == 409
    assert f["session"].yaw_preview.selection_epoch == 0


def test_session_private_appearance_wiring_recovers_a_new_track_without_new_selection(fly):
    f = fly
    first = f["select"]().json()["state"]["yaw_preview"]
    f["observe"](1.1, [])
    assert f["client"].get(STATE).json()["yaw_preview"]["phase"] == "paused"
    f["observe"](2.1, [person(81, .72)])
    assert f["client"].get(STATE).json()["yaw_preview"]["phase"] == "paused"
    f["observe"](2.2, [person(81, .73)])
    recovered = f["client"].get(STATE).json()["yaw_preview"]
    assert recovered["phase"] == "tracking" and recovered["target_id"] == 81
    assert recovered["selection_id"] == 7
    assert recovered["selection_epoch"] == first["selection_epoch"]


def test_recovery_attempts_reach_run_log_through_app_and_session(fly, tmp_path):
    from argos.console.yaw_assist import YawAssistService

    f = fly
    service = YawAssistService("/dev/not-opened", 8080, directory=tmp_path / "run")
    create_app(session=f["session"], vision=f["vision"], yaw_service=service)
    try:
        assert f["select"]().status_code == 200
        f["observe"](1.1, [])
        f["observe"](1.2, [person(81, .72)])
        for _ in range(5):
            f["client"].get(STATE)  # Display/source polling is not another attempt.
        f["observe"](1.3, [person(81, .73)])
        assert f["client"].get(STATE).json()["yaw_preview"]["target_id"] == 81
    finally:
        service.close()
    records = [json.loads(line)["event"] for line in
               (tmp_path / "run/recovery.jsonl").read_text().splitlines()]
    pending = [record for record in records if record["reason"] == "pending_second_image"]
    accepted = [record for record in records if record["reason"] == "accepted_appearance"]
    assert len(pending) == len(accepted) == 1
    assert accepted[0]["accepted"] is True
    assert accepted[0]["similarity"] == pytest.approx(1.)
    assert accepted[0]["dx"] == pytest.approx(.03)
    assert accepted[0]["dy"] == pytest.approx(0.)
    assert accepted[0]["width_ratio"] == pytest.approx(1.)
    assert accepted[0]["height_ratio"] == pytest.approx(1.)
    assert "appearance" not in accepted[0] and "appearances" not in accepted[0]


def test_python_source_posts_to_real_asgi_endpoint_with_matching_origin(fly, monkeypatch):
    """Exercise source HTTP headers/body and actual endpoint/validator together."""
    f = fly

    class Response:
        def __init__(self, response):
            self.response = response
            self.status = response.status_code

        def getheader(self, key, default=None):
            return self.response.headers.get(key, default)

        def read(self, maximum):
            return self.response.content[:maximum]

    class Connection:
        def __init__(self, host, port, *, timeout):
            assert (host, port) == ("127.0.0.1", 8080)

        def request(self, method, path, body=None, *, headers):
            self.response = f["client"].request(method, path, content=body, headers=headers)

        def getresponse(self):
            return Response(self.response)

        def close(self):
            pass

    monkeypatch.setattr(yaw_stream_source.http.client, "HTTPConnection", Connection)
    times = iter([100., 100.01, 100.02, 100.1, 100.11, 100.12])
    source = yaw_stream_source.YawSource(clock=lambda: next(times))
    assert not source.read().valid
    f["observe"](1.1)
    result = source.select_center("1234abcd:0")
    assert result.valid and result.selection_key[2] == 7
    assert f["session"].yaw_preview.state(1.1)["phase"] == "tracking"
