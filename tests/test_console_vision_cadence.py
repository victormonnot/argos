"""Queue servicing must not waste the freshness margin of a modest CPU."""
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
import asyncio

import pytest

from argos.console.config import ConsoleConfig
from argos.console.video import VideoSample
from argos.console.vision import VisionService
from argos.console.yaw_preview import YawPreview
from test_console_vision import Camera, Context


@pytest.mark.parametrize("camera_phase_ms", [0, 7, 23, 49, 66])
@pytest.mark.parametrize("worker_ms", [80, 110, 150, 165])
@pytest.mark.parametrize("max_hz", [5, 8])
def test_cpu_cadence_keeps_selected_image_fresh(camera_phase_ms, worker_ms, max_hz):
    # Deterministic workload: 15 Hz camera, serial worker, independent phase.
    # worker_ms covers the entire simulated job, not measured DNN inference.
    now = [0.]
    camera = Camera(lambda: now[0])
    session = SimpleNamespace(run_id="run", video_source_id="camera", video=camera,
                              config=ConsoleConfig(vision_hz=max_hz), clock=lambda: now[0])
    vision = VisionService(Path("fixture.onnx"), max_hz=max_hz,
                           wall_clock=lambda: now[0], process_context=Context())
    preview = YawPreview(True)
    vision.start()
    vision._outgoing.put_nowait(("ready", None))
    poll_ms = round(1000 * vision.poll_interval)
    job = None
    camera_number = -1
    selected = False
    submitted = []
    try:
        for ms in range(5000):
            now[0] = ms / 1000
            number = (ms - camera_phase_ms) * 15 // 1000
            if number > camera_number:
                camera_number = number
                camera.sample = VideoSample(b"jpeg", number + 2, now[0])
            if job is not None and ms >= job[0]:
                vision._outgoing.put_nowait(("result", (job[1], {
                    "width": 640, "height": 480, "inference_ms": float(worker_ms),
                    "detections": [{"box": [.6, .2, .2, .6], "confidence": .9}],
                }, [None])))
                job = None
            if ms % poll_ms == 0:
                vision.tick(session)
                try:
                    identifier, _ = vision._incoming.get_nowait()
                    assert job is None, "Never queue a second inference"
                    job = ms + worker_ms, identifier
                    submitted.append(now[0])
                except Empty:
                    pass
                frame = vision.frame(session)
                if frame is not None:
                    preview.observe(dict(run_id="run", video_id="camera",
                        sequence=frame.sample.sequence, received_at=frame.sample.received_at,
                        width=640, height=480, detections=frame.result["detections"]), now[0])
                    if not selected:
                        preview.select(frame.result["detections"][0]["track_id"],
                                       revision=preview.revision, now=now[0])
                        selected = True
            if selected:
                # A read may occur between any two receive ticks.
                state = preview.state(now[0])
                assert state["phase"] == "tracking", (ms, state)
                assert state["frame_age_s"] <= .45
        assert selected
        assert vision.state(session)["max_hz"] == max_hz
        assert all(b - a >= 1 / max_hz - 1e-9 for a, b in zip(submitted, submitted[1:]))
        if max_hz == 8 and worker_ms <= 110:
            assert len(submitted) >= 37, "The requested rate must reach the actual scheduler"
        if max_hz == 5:
            assert len(submitted) <= 26
    finally:
        asyncio.run(vision.aclose())


def test_disabled_or_failed_vision_does_not_require_fast_polling():
    assert VisionService(None).poll_interval == .05
    vision = VisionService(Path("fixture.onnx"))
    assert vision.poll_interval == .01
    vision._fail("fixture failure")
    assert vision.poll_interval == .05
