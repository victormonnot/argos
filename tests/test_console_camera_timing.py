"""A camera publishing after a caller's clock read must not fake an outage."""
from pathlib import Path

import pytest

from argos.backends.vision_bench_source import PreviewValidator
from argos.console.video import VideoStore
from test_console_yaw_preview_api import preview  # actual session/API fixture


class PublishBeforeLock:
    """Deterministically interleave a new acquisition before the next read.

    The producer uses the real acceptance path and advances the shared clock.
    Legacy readers have already captured their timestamp at this point; live
    readers must capture theirs only once the store is locked.
    """
    def __init__(self, store, now):
        self.store, self.now = store, now
        self.lock = store._lock
        self.pending = False

    def __enter__(self):
        if self.pending:
            self.pending = False
            self.now[0] += .001
            assert self.store.accept_raw(width=2, height=2, step=6,
                pixel_format="RGB_INT8", data=bytes(12), received_at=self.now[0])
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


def setup(f):
    f["vision"].model_path = Path("synthetic.onnx")
    f["vision"]._ready = True
    assert f["select"]().status_code == 200
    f["now"][0] += .1
    gate = PublishBeforeLock(f["session"].video, f["now"])
    f["session"].video._lock = gate
    gate.pending = True
    return gate


def test_acquisition_between_clock_and_observation_keeps_selected_preview(preview):
    f = preview
    gate = setup(f)
    previous = f["vision"]._frame
    f["session"]._observe_vision()
    state = f["session"].state()
    assert not gate.pending and f["vision"]._frame is previous
    assert state["yaw_preview"]["phase"] == "tracking"
    assert state["yaw_preview"]["target_id"] == 1
    assert state["yaw_preview"]["frame_received_at"] == .1
    assert state["yaw_preview"]["frame_age_s"] == pytest.approx(.101)


def test_live_api_snapshot_is_consistent_for_bench_after_new_publication(preview):
    f = preview
    setup(f)
    state = f["client"].get("/api/state").json()
    assert state["video"]["state"] == "recent"
    assert state["video"]["received_at"] <= state["at"]
    assert state["video"]["rx_age_s"] == pytest.approx(0.)
    value = PreviewValidator().validate(state, 50., 50.001)
    assert value.target_id == 1
    assert value.received_at == .1  # No redating the already analyzed image.


def test_live_raw_frame_endpoint_survives_concurrent_publication(preview):
    f = preview
    setup(f)
    result = f["client"].get("/api/frame.jpg")
    assert result.status_code == 200
    assert float(result.headers["X-Frame-Received-At"]) == pytest.approx(.201)


def test_actual_capture_failure_still_stops_and_cannot_resume_preview(preview):
    f = preview
    setup(f)
    f["session"].video.fail("No such device")
    f["session"]._observe_vision()
    state = f["session"].state()["yaw_preview"]
    assert state["phase"] == "stopped" and state["yaw"] == 0
    f["now"][0] += .01
    # Reopening gives the camera a new store/source. Its first valid image must
    # not restore a target whose previous source was lost.
    session = f["session"]
    session.video = VideoStore(source="device", endpoint="/dev/video2", clock=session.clock)
    session.video_source_id = "reopened-camera"
    f["observe"]()
    assert f["session"].state()["yaw_preview"]["phase"] == "stopped"
