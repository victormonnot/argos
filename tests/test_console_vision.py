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
        self.kwargs = kwargs
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


def complete(vision, session, identifier, *, detections=None, appearances=None, width=640, height=480):
    result = {"width": width, "height": height, "inference_ms": 45.,
              "detections": detections if detections is not None else [{"box": [.2, .3, .1, .4], "confidence": .8}]}
    if appearances is None:
        appearances = [None] * len(result["detections"])
    vision._outgoing.put_nowait(("result", (identifier, result, appearances)))
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
    complete(vision, source, identifier, appearances=[descriptor()])
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
    metadata = json.loads(response.headers["x-vision-result"])
    assert set(metadata) == {"width", "height", "inference_ms", "detections"}
    assert set(metadata["detections"][0]) == {"box", "confidence", "track_id"}
    assert metadata["detections"][0]["box"] == [.2, .3, .1, .4]
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
    vision._outgoing.put_nowait(("result", (identifier, bad, [])))
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


def test_selected_model_variant_reaches_process_worker_and_status(runtime):
    from argos.console.vision import _worker
    _, session, _ = runtime
    vision = VisionService(Path("chosen.onnx"), variant="s", threads=4, process_context=Context())
    try:
        vision.start()
        assert vision._process.kwargs["target"] is _worker
        assert vision._process.kwargs["args"] == ("chosen.onnx", vision._incoming, vision._outgoing, "s", 4)
        state = vision.state(session)
        assert (state["model"], state["variant"], state["input_size"]) == ("YOLOX-S", "s", 640)
        assert state["threads"] == 4
    finally:
        asyncio.run(vision.aclose())


def test_worker_constructs_only_the_explicit_variant(monkeypatch):
    from argos.perception import appearance, yolox
    from argos.console.vision import _worker
    seen = []
    monkeypatch.setattr(yolox, "YoloXPersonDetector",
                        lambda path, *, variant, threads: seen.append((path, variant, threads)))
    monkeypatch.setattr(appearance, "AppearanceEncoder", lambda: object())
    incoming, outgoing = Queue(), Queue()
    incoming.put(None)
    _worker("chosen.onnx", incoming, outgoing, "s", 4)
    assert seen == [("chosen.onnx", "s", 4)]
    assert outgoing.get_nowait() == ("ready", None)


def descriptor(index=0):
    return tuple(float(i == index) for i in range(208))


def narrow_person(x):
    return {"box": [x, .3, .04, .2], "confidence": .8}


def next_job(runtime, stamp):
    now, session, vision = runtime
    now[0] = stamp
    session.video.sample = VideoSample(b"next", session.video.sample.sequence + 1, stamp)
    vision.tick(session)
    return vision._incoming.get_nowait()[0]


def test_only_matching_accepted_job_can_supply_appearance(runtime):
    _, session, vision = runtime
    identifier, _ = ready(vision, session)
    complete(vision, session, identifier, detections=[narrow_person(.3)], appearances=[descriptor()])
    original = vision.frame(session).result["detections"][0]["track_id"]
    identifier = next_job(runtime, .25)
    # A mismatched job with contradictory appearance cannot poison the reference.
    complete(vision, session, identifier + 100, detections=[narrow_person(.39)], appearances=[descriptor(1)])
    assert vision.frame(session).sample.sequence == 1
    assert vision._pending is not None
    complete(vision, session, identifier, detections=[narrow_person(.39)], appearances=[descriptor()])
    frame = vision.frame(session)
    assert frame.result["detections"] == [{**narrow_person(.39), "track_id": original}]
    assert set(frame.result) == {"width", "height", "inference_ms", "detections"}
    assert set(frame.result["detections"][0]) == {"box", "confidence", "track_id"}


def test_rejected_stale_result_cannot_refresh_appearance_or_identity(runtime):
    now, session, vision = runtime
    identifier, _ = ready(vision, session)
    complete(vision, session, identifier, detections=[narrow_person(.3)], appearances=[descriptor()])
    original = vision.frame(session).result["detections"][0]["track_id"]
    identifier = next_job(runtime, .25)
    now[0] = 1.3
    session.video.sample = VideoSample(b"fresh", 3, 1.3)
    complete(vision, session, identifier, detections=[narrow_person(.39)], appearances=[descriptor()])
    assert vision.frame(session) is None
    assert vision._processed == 1
    fresh_id, _ = vision._incoming.get_nowait()
    complete(vision, session, fresh_id, detections=[narrow_person(.39)], appearances=[descriptor()])
    assert vision.frame(session).result["detections"][0]["track_id"] != original


@pytest.mark.parametrize("change", ["run", "source", "dimensions"])
def test_appearance_identity_and_selection_do_not_cross_context_change(runtime, change):
    _, session, vision = runtime
    identifier, _ = ready(vision, session)
    complete(vision, session, identifier, detections=[narrow_person(.3)], appearances=[descriptor()])
    original = vision.frame(session).result["detections"][0]["track_id"]
    if change == "run":
        session.run_id = "replacement"
    elif change == "source":
        session.video_source_id = "replacement"
    identifier = next_job(runtime, .25)
    complete(vision, session, identifier, detections=[narrow_person(.3)], appearances=[descriptor()],
             width=800 if change == "dimensions" else 640)
    frame = vision.frame(session)
    assert frame.result["detections"][0]["track_id"] != original
    assert len(vision._selection_history) == 1
    assert all(original not in entry[3] for entry in vision._selection_history)


@pytest.mark.parametrize("bad", [[], [None, None], [[0.] * 208], [[float("nan")] * 208],
                                 [[1.] * 209], ["invalid"]])
def test_malformed_appearance_is_contained_at_optional_vision_boundary(runtime, bad):
    _, session, vision = runtime
    identifier, _ = ready(vision, session)
    complete(vision, session, identifier, appearances=bad)
    assert vision.frame(session) is None
    assert vision.state(session)["state"] == "error"
    assert vision._process.terminated
    vision.tick(session)  # no repeated exception escapes to flight servicing


def test_worker_pairs_current_jpeg_boxes_dimensions_and_private_descriptor(monkeypatch):
    from argos.perception import appearance, yolox
    from argos.console.vision import _worker
    observed = []
    result = {"width": 640, "height": 480, "inference_ms": 1., "detections": [narrow_person(.3)]}
    class Detector:
        def __init__(self, *args, **kwargs):
            pass

        def detect(self, jpeg):
            observed.append(("detect", jpeg))
            return result

    class Encoder:
        def encode(self, jpeg, detections, *, width, height):
            observed.append(("encode", jpeg, detections, width, height))
            return [descriptor()]

    monkeypatch.setattr(yolox, "YoloXPersonDetector", Detector)
    monkeypatch.setattr(appearance, "AppearanceEncoder", Encoder)
    incoming, outgoing = Queue(), Queue()
    incoming.put((17, b"exact camera image"))
    incoming.put(None)
    _worker("model.onnx", incoming, outgoing)
    assert outgoing.get_nowait() == ("ready", None)
    assert outgoing.get_nowait() == ("result", (17, result, [descriptor()]))
    assert observed == [("detect", b"exact camera image"),
                        ("encode", b"exact camera image", result["detections"], 640, 480)]


@pytest.mark.parametrize("bad_identifier", [True, 1., 0, -1, "1"])
def test_worker_job_identifier_requires_exact_positive_integer(runtime, bad_identifier):
    _, session, vision = runtime
    identifier, _ = ready(vision, session)
    assert identifier == 1
    complete(vision, session, bad_identifier)
    assert vision.frame(session) is None
    assert vision._processed == 0
    assert "invalid job identifier" in vision.state(session)["detail"]


def test_missing_worker_appearance_list_is_not_a_valid_no_descriptor_reply(runtime):
    _, session, vision = runtime
    identifier, _ = ready(vision, session)
    result = {"width": 640, "height": 480, "inference_ms": 1., "detections": [narrow_person(.3)]}
    vision._outgoing.put_nowait(("result", (identifier, result, None)))
    vision.tick(session)
    assert vision.frame(session) is None
    assert "appearance list" in vision.state(session)["detail"]


def test_model_variant_survives_source_changes_and_reaches_app(tmp_path):
    pytest.importorskip("fastapi")
    from argos.console.app import create_app
    config = ConsoleConfig(vision_model=tmp_path / "model.onnx", vision_variant="s",
                           vision_threads=4, recordings_dir=tmp_path)
    replaced = config.with_sources(config.public())
    assert replaced.vision_variant == "s" and "vision_variant" not in config.public()
    assert replaced.vision_threads == 4 and "vision_threads" not in config.public()
    app = create_app(replaced)
    assert app.state.vision.model.variant == "s"
    assert app.state.vision.threads == 4
    assert ConsoleConfig().vision_variant == "tiny"
    assert ConsoleConfig().vision_threads == 2


@pytest.mark.parametrize("variant", ["custom", "S", None, [], True])
def test_unknown_vision_variant_is_rejected_in_config(variant):
    with pytest.raises(ValueError, match="variant must be"):
        ConsoleConfig(vision_variant=variant)


@pytest.mark.parametrize("threads", [0, 7, -1, True, None, 2.0, "4", []])
def test_invalid_thread_limit_is_rejected_in_config_and_service(threads):
    with pytest.raises(ValueError, match="threads must be an integer between 1 and 6"):
        ConsoleConfig(vision_threads=threads)
    with pytest.raises(ValueError, match="threads must be an integer between 1 and 6"):
        VisionService(None, threads=threads)


def test_console_cli_passes_explicit_variant_to_config(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from argos.console import __main__ as cli
    from argos.console import app
    import sys
    seen = []
    monkeypatch.setattr(sys, "argv", ["argos.console", "--vision-model", str(tmp_path / "s.onnx"),
                                     "--vision-variant", "s", "--vision-threads", "4"])
    monkeypatch.setattr(app, "create_app", lambda config: seen.append(config) or "fake-app")
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **kw: None))
    cli.main()
    assert len(seen) == 1 and seen[0].vision_variant == "s"
    assert seen[0].vision_threads == 4
