"""Perception never queues obsolete images or crosses a camera/run boundary."""
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
import asyncio

import pytest

from argos.console.config import ConsoleConfig
from argos.console.video import VideoSample
from argos.console.vision import VisionService, INFERENCE_TIMEOUT, START_TIMEOUT


class Camera:
    def __init__(self):
        self.sample = VideoSample(b"original", 1, 0.)
        self.failed = False

    def latest(self, now):
        return None if self.failed or now - self.sample.received_at > 1 else self.sample


class Process:
    pid = 123

    def __init__(self, **kwargs):
        self.alive = True
        self.terminated = False
        self.closed = False

    def start(self):
        pass

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def join(self, timeout):
        pass

    def close(self):
        self.closed = True


class LocalQueue(Queue):
    def cancel_join_thread(self):
        pass

    def close(self):
        self.closed = True


class Context:
    Queue = LocalQueue
    Process = Process


@pytest.fixture
def runtime():
    now = [0.]
    session = SimpleNamespace(run_id="run-a", video_source_id="video-a", video=Camera(),
                              config=ConsoleConfig(), clock=lambda: now[0])
    vision = VisionService(Path("model.onnx"), wall_clock=lambda: now[0], process_context=Context())
    vision.start()
    yield now, session, vision
    asyncio.run(vision.aclose())


def ready(vision, session):
    vision._outgoing.put_nowait(("ready", None))
    vision.tick(session)
    return vision._incoming.get_nowait()


def complete(vision, session, identifier, *, detections=None):
    result = {"width": 640, "height": 480, "inference_ms": 45.,
              "detections": detections if detections is not None else [{"box": [.2, .3, .1, .4], "confidence": .8}]}
    vision._outgoing.put_nowait(("result", (identifier, result)))
    vision.tick(session)


def test_result_keeps_original_image_and_source_receipt_when_camera_advances(runtime):
    now, session, vision = runtime
    identifier, jpeg = ready(vision, session)
    assert jpeg == b"original"
    now[0] = .1
    session.video.sample = VideoSample(b"newer", 2, .1)
    complete(vision, session, identifier)
    candidate = vision.frame(session)
    assert candidate.sample.jpeg == b"original"
    assert candidate.sample.sequence == 1 and candidate.sample.received_at == 0.
    assert candidate.context == ("run-a", "video-a")
    assert candidate.result["detections"][0]["track_id"] >= 1
    assert vision.state(session)["frame_age_s"] == .1
    assert vision.state(session)["processed"] == 1


def test_one_pending_job_drops_intermediate_frames_and_caps_submission_rate(runtime):
    now, session, vision = runtime
    identifier, _ = ready(vision, session)
    for number in range(2, 30):
        now[0] += .01
        session.video.sample = VideoSample(b"next", number, now[0])
        vision.tick(session)
        assert vision._incoming.empty()
    complete(vision, session, identifier)
    next_id, _ = vision._incoming.get_nowait()
    assert next_id == identifier + 1
    assert vision._pending[1].sample.sequence == 29
    complete(vision, session, next_id)
    now[0] += .01
    session.video.sample = VideoSample(b"newest", 30, now[0])
    vision.tick(session)
    assert vision._incoming.empty()


@pytest.mark.parametrize("attribute,value", [("run_id", "run-b"), ("video_source_id", "video-b")])
def test_inflight_result_cannot_cross_source_or_run_change(runtime, attribute, value):
    now, session, vision = runtime
    identifier, _ = ready(vision, session)
    setattr(session, attribute, value)
    now[0] = .3
    session.video.sample = VideoSample(b"new source", 1, .3)
    complete(vision, session, identifier)
    assert vision.frame(session) is None
    assert vision.state(session)["processed"] == 0
    next_id, _ = vision._incoming.get_nowait()
    complete(vision, session, next_id)
    assert vision.frame(session).sample.jpeg == b"new source"


def test_cached_result_expires_despite_new_live_frames_and_failed_camera(runtime):
    now, session, vision = runtime
    identifier, _ = ready(vision, session)
    complete(vision, session, identifier)
    assert vision.frame(session)
    session.video.failed = True
    assert vision.frame(session) is None
    session.video.failed = False
    now[0] = 1.01
    session.video.sample = VideoSample(b"live", 2, now[0])
    assert vision.frame(session) is None
    assert vision.state(session)["state"] == "stale"
    assert vision.state(session)["tracks"] == 0


def test_slow_result_never_renews_receipt_or_revives_stale_track(runtime):
    now, session, vision = runtime
    identifier, _ = ready(vision, session)
    now[0] = 1.1
    session.video.sample = VideoSample(b"live", 2, 1.1)
    complete(vision, session, identifier)
    assert vision.frame(session) is None
    assert vision.state(session)["processed"] == 0


@pytest.mark.parametrize("ready_first,timeout", [(False, START_TIMEOUT), (True, INFERENCE_TIMEOUT)])
def test_hung_model_is_terminated_without_waiting_on_event_loop(runtime, ready_first, timeout):
    now, session, vision = runtime
    if ready_first:
        ready(vision, session)
    now[0] = timeout + .01
    vision.tick(session)
    assert vision.state(session)["state"] == "error"
    assert vision._process.terminated
    assert vision.frame(session) is None


def test_worker_crash_and_reported_errors_clear_results(runtime):
    _, session, vision = runtime
    identifier, _ = ready(vision, session)
    complete(vision, session, identifier)
    vision._process.alive = False
    vision.tick(session)
    assert vision.state(session)["state"] == "error"
    assert vision.frame(session) is None


def test_worker_model_load_error_is_inspectable(runtime):
    _, session, vision = runtime
    vision._outgoing.put_nowait(("error", "Vision unavailable: incorrect model checksum"))
    vision.tick(session)
    assert "checksum" in vision.state(session)["detail"]
    assert vision._process.terminated


def test_vision_is_disabled_by_default_without_starting_a_process():
    vision = VisionService(None, process_context=SimpleNamespace())
    vision.start()
    assert vision._process is None
    config = ConsoleConfig(vision_model=Path("model.onnx"))
    assert config.with_sources(config.public()).vision_model == config.vision_model
    assert "vision_model" not in config.public()


def test_http_pairs_boxes_and_jpeg_and_removes_them_on_context_change(runtime):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from argos.console.app import create_app
    from argos.console.session import ConsoleSession
    now, source, vision = runtime
    identifier, _ = ready(vision, source)
    complete(vision, source, identifier)
    session = ConsoleSession(ConsoleConfig(), clock=lambda: now[0])
    session.run_id, session.video_source_id = source.run_id, source.video_source_id
    session.video = source.video
    # No lifespan: deliberately retain the test-owned worker and acquired frame.
    client = TestClient(create_app(session=session, vision=vision))
    response = client.get("/api/vision/frame.jpg")
    assert response.status_code == 200 and response.content == b"original"
    assert response.headers["x-frame-sequence"] == "1"
    assert response.headers["x-frame-received-at"] == "0.0"
    import json
    assert json.loads(response.headers["x-vision-result"])["detections"][0]["box"] == [.2, .3, .1, .4]
    session.video_source_id = "replacement"
    assert client.get("/api/vision/frame.jpg").status_code == 503
    client.close()


def test_queue_failure_is_contained_at_optional_vision_boundary(runtime):
    _, session, vision = runtime
    def broken():
        raise OSError("worker pipe closed")
    vision._outgoing.get_nowait = broken
    vision.tick(session)
    assert vision.state(session)["state"] == "error"
    assert "pipe closed" in vision.state(session)["detail"]
    vision.tick(session)  # subsequent event-loop ticks remain callable


@pytest.mark.parametrize("bad", [None, {"detections": []},
    {"width": 640, "height": 480, "inference_ms": float("nan"), "detections": []},
    {"width": 640, "height": 480, "inference_ms": 1., "detections": [{"box": [.9, 0, .3, .2], "confidence": .8}]}])
def test_invalid_worker_result_cannot_break_state_serialization(runtime, bad):
    _, session, vision = runtime
    identifier, _ = ready(vision, session)
    vision._outgoing.put_nowait(("result", (identifier, bad)))
    vision.tick(session)
    assert vision.frame(session) is None
    assert vision.state(session)["state"] == "error"


def test_distinct_sequence_with_equal_receipt_does_not_disable_or_refresh_tracking(runtime):
    now, session, vision = runtime
    identifier, _ = ready(vision, session)
    complete(vision, session, identifier)
    now[0] = .3
    session.video.sample = VideoSample(b"same clock tick", 2, 0.)
    vision.tick(session)
    assert vision._incoming.empty()
    assert vision.state(session)["state"] == "recent"
    assert vision.frame(session).sample.sequence == 1
