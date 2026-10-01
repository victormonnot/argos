"""Per-take evidence uses real bounded writers and explicit clock domains."""
import json
from threading import Event
import time

import pytest

from argos.console.camera_recording import CameraRecorder
from argos.console.filming import FilmingCapture, assistance_view, label_value
from argos.console.video import VideoStore
from argos.console.vision import AnalyzedFrame
from argos.console.yaw_assist import YawAssistService


class Clock:
    def __init__(self, now=10.):
        self.now = now

    def __call__(self):
        return self.now


class PausedFilming(FilmingCapture):
    def __init__(self, *args, **kwargs):
        self.release = Event()
        super().__init__(*args, **kwargs)

    def _run(self):
        assert self.release.wait(3.)
        super()._run()


def make(path, *, cls=FilmingCapture, clock=None, **kwargs):
    clock = clock or Clock()
    return cls(path / "take.flight", identifier="a" * 32, run_id="b" * 32,
               started_at=10., clock=clock, **kwargs)


def finish(capture, now=14.):
    if isinstance(capture, PausedFilming):
        capture.release.set()
    return capture.stop(now, timeout=2.)


def rows(capture):
    return [json.loads(line) for line in (capture.directory / "events.jsonl").read_text().splitlines()]


def status(*, at=100., mode="D", state="A", yaw_phase="A", pitch_phase="A", pitch_valid=True):
    sample = dict(schema_version=1, session="1234abcd", generation=4, ticket=9, ack=7,
                  state=state, cause="A" if state == "A" else "M", mode=mode,
                  yaw_phase=yaw_phase, pitch_phase=pitch_phase, received_at=at,
                  sticks=dict(roll=31, pitch=-45, throttle=590, yaw=12),
                  lua_outputs={"yaw": dict(value=40, valid=yaw_phase == "A" and state == "A"),
                               "pitch": dict(value=-20 if pitch_valid else 0, valid=pitch_valid)})
    return dict(connected=True, session="1234abcd", generation=4, radio_state=state,
                radio_mode=mode, radio_yaw_phase=yaw_phase, radio_pitch_phase=pitch_phase,
                pilot_sample=sample, pilot_sample_count=1)


@pytest.mark.parametrize("received,now,connected,expected", [
    (100., 100.1, True, True), (100., 100.351, True, False),
    (101., 100., True, False), (float("nan"), 100., True, False),
    (100., 100., False, False), (None, 100., True, False),
])
def test_axis_view_ages_actual_receipt_not_republished_status(received, now, connected, expected):
    value = status(at=received)
    value["connected"] = connected
    view = assistance_view(value, now)
    assert view["assistance"]["fresh"] is expected
    assert view["assistance"]["yaw"]["state"] == ("assisted" if expected else "unknown")
    assert "flight-controller" in view["assistance"]["meaning"]


def test_axis_view_distinguishes_temporary_manual_waiting_and_invalid_pitch():
    value = status(yaw_phase="M", pitch_valid=False)
    view = assistance_view(value, 100.1)["assistance"]
    assert view["selected_mode"] == "D"
    assert view["yaw"]["state"] == "manual"
    assert view["pitch"]["state"] == "paused"
    value = status(yaw_phase="M")
    view = assistance_view(value, 100.1)["assistance"]
    assert view["yaw"]["state"] == "manual" and view["pitch"]["state"] == "assisted"
    value = status(pitch_phase="R", pitch_valid=False)
    assert assistance_view(value, 100.1)["assistance"]["pitch"]["state"] == "waiting"


def test_newer_radio_ownership_overrides_fresh_but_old_sample():
    value = status()
    value["radio_yaw_phase"] = "M"
    view = assistance_view(value, 100.1)["assistance"]
    assert view["yaw"]["state"] == "unknown" and view["pitch"]["state"] == "assisted"
    value["generation"] += 1
    view = assistance_view(value, 100.1)["assistance"]
    assert not view["fresh"] and view["selected_mode"] is None
    assert view["pitch"]["state"] == "unknown"


def test_missing_sample_does_not_infer_sticks_or_authority_from_sent_values():
    value = dict(connected=True, radio_state="A", last_sent_valid=True,
                 last_sent_pitch_valid=True, last_sent_command={"yaw": {"valid": True, "value": 100}})
    view = assistance_view(value, 100.)
    assert not view["pilot_sample_fresh"] and view["pilot_sample_age_s"] is None
    assert view["assistance"]["yaw"]["state"] == "unknown"
    assert view["assistance"]["pitch"]["value"] is None


def test_yaw_only_and_persistent_manual_are_distinct_from_distance():
    value = status(mode="Y", pitch_phase="N", pitch_valid=False)
    # FLY reports no explicit per-axis/mode fields in its older AY1 status.
    for key in ("radio_mode", "radio_yaw_phase", "radio_pitch_phase"):
        value.pop(key)
    view = assistance_view(value, 100.1)["assistance"]
    assert view["selected_mode"] == "Y" and view["pitch"]["state"] == "manual"
    value = status(mode="N", state="M", yaw_phase="N", pitch_phase="N", pitch_valid=False)
    view = assistance_view(value, 100.1)["assistance"]
    assert view["selected_mode"] == "M"
    assert view["yaw"]["state"] == view["pitch"]["state"] == "manual"


def test_complete_take_links_camera_vision_radio_and_clock_anchor_without_rebasing(tmp_path):
    console_clock, host_clock = Clock(), Clock(1000.)
    capture = make(tmp_path, cls=PausedFilming, clock=console_clock,
                   label=" battery 2 ", manifest={"runtime_sha256": "verified-by-launcher"})
    service = YawAssistService("/dev/not-opened", 8080, clock=host_clock)
    video = VideoStore(source="device", endpoint="/dev/video2", clock=console_clock)
    capture.attach_video(video, "c" * 32)
    capture.attach_radio(service)
    try:
        console_clock.now = 10.2
        assert video.accept_raw(width=2, height=2, step=6, pixel_format="RGB_INT8",
                                data=bytes(12), received_at=10.2)
        frame = video.latest(10.2)
        candidate = AnalyzedFrame(frame, ("b" * 32, "c" * 32),
            dict(width=2, height=2, detections=[], inference_ms=12., private_blob="not-recorded"))
        assert capture.record_vision(candidate, source_id="c" * 32, preview={"phase": "tracking"})
        assert not capture.record_vision(candidate, source_id="c" * 32, preview={"phase": "tracking"})
        service._publish(status(at=999.9))
        result = finish(capture)
        assert result["complete"] and result["writer_stopped"]
        assert result["label"] == "battery 2" and result["pilot_samples"] == 1
        events = rows(capture)
        assert [item["kind"] for item in events] == ["vision", "radio"]
        vision, radio = events
        assert vision["at"] == radio["at"] == 10.2
        assert vision["image_received_at"] == 10.2 and vision["image_elapsed_s"] == pytest.approx(.2)
        assert "private_blob" not in vision["result"]
        assert radio["clock_anchor"] == dict(console_before=10.2, host_monotonic_at=1000., console_after=10.2)
        assert radio["status"]["pilot_sample"]["received_at"] == 999.9
        assert radio["status"]["assistance"]["yaw"]["state"] == "assisted"
        camera_rows = [json.loads(row) for row in (capture.directory / "camera.frames.jsonl").read_text().splitlines()]
        assert camera_rows[0]["source_id"] == vision["video_id"]
        assert camera_rows[0]["sequence"] == vision["frame_sequence"]
        assert (capture.directory / "camera.mjpeg").read_bytes() == frame.jpeg
        manifest = json.loads((capture.directory / "manifest.json").read_text())
        assert manifest["complete"] and manifest["run_id"] == "b" * 32
        assert manifest["provenance"]["runtime_sha256"] == "verified-by-launcher"
        assert not service._status_sinks
        service._publish(status(at=1000.))
        assert len(rows(capture)) == 2
    finally:
        finish(capture)
        service.close()


def test_queue_pressure_and_mutex_contention_never_block_producers_and_are_counted(tmp_path):
    capture = make(tmp_path, cls=PausedFilming, max_queue_bytes=1)
    try:
        before = time.monotonic()
        assert not capture.record_radio(status())
        with capture._lock:
            assert not capture.record_radio(status())
        assert time.monotonic() - before < .2
        result = finish(capture)
        assert result["dropped_events"] == 2 and result["contention_drops"] == {"radio": 1, "vision": 0}
        assert result["events"] == 0 and not result["complete"]
    finally:
        finish(capture)


@pytest.mark.parametrize("limits,expected_events", [({"max_events": 1}, 1), ({"max_log_bytes": 1}, 0)])
def test_event_file_limits_preserve_written_prefix_and_close_workers(tmp_path, limits, expected_events):
    capture = make(tmp_path, cls=PausedFilming, **limits)
    try:
        assert capture.record_radio(status())
        assert capture.record_radio(status(at=100.1))
        result = finish(capture)
        assert result["writer_stopped"] and result["state"] == "error"
        assert "limit" in result["error"] and result["events"] == expected_events
        assert result["failed_events"] == 2 - expected_events
        assert len(rows(capture)) == expected_events
        assert result["pending_events"] == 0 and not result["complete"]
    finally:
        finish(capture)


def test_stop_is_nonblocking_and_completion_waits_for_camera_writer(tmp_path):
    gate = Event()

    class HeldCamera(CameraRecorder):
        def _run(self):
            assert gate.wait(3.)
            super()._run()

    capture = make(tmp_path, camera_factory=HeldCamera)
    try:
        before = time.monotonic()
        result = capture.stop(12., timeout=0)
        assert time.monotonic() - before < .2
        assert result["state"] == "finalizing" and not result["writer_stopped"]
        assert not result["complete"] and not capture.record_radio(status())
    finally:
        gate.set()
        result = finish(capture)
    assert result["writer_stopped"] and result["ended_at"] == 12.


def test_stop_timestamp_cannot_precede_accepted_events_or_last_camera_receipt(tmp_path):
    clock = Clock(13.)
    capture = make(tmp_path, cls=PausedFilming, clock=clock)
    video = VideoStore(source="device", endpoint="/dev/video2", clock=clock)
    capture.attach_video(video, "c" * 32)
    try:
        assert capture.record_radio(status())
        clock.now = 13.2
        assert video.accept_raw(width=2, height=2, step=6, pixel_format="RGB_INT8",
                                data=bytes(12), received_at=13.2)
        # A lifecycle timestamp may be sampled before the producer finishes.
        result = finish(capture, now=12.)
        assert result["complete"] and result["ended_at"] == 13.2
        assert result["ended_at"] >= rows(capture)[0]["at"]
        manifest = json.loads((capture.directory / "manifest.json").read_text())
        assert manifest["ended_at"] == result["camera"]["ended_at"]
    finally:
        finish(capture)


def test_status_sink_exception_is_counted_without_changing_radio_observation():
    service = YawAssistService("/dev/not-opened", 8080, clock=Clock(100.))
    def broken(_):
        raise OSError("disk unavailable")
    service.add_status_sink(broken)
    value = status()
    service._publish(value)
    result = service.snapshot()
    assert result["radio_state"] == "A" and result["recording_sink_errors"] == 1
    assert result["assistance"]["yaw"]["state"] == "assisted"
    service.close()


@pytest.mark.parametrize("bad_clock", [9., float("nan"), float("inf")])
def test_invalid_event_clock_closes_writers_instead_of_leaving_take_finalizing(tmp_path, bad_clock):
    clock = Clock()
    capture = make(tmp_path, clock=clock)
    try:
        clock.now = bad_clock
        assert not capture.record_radio(status())
        capture._thread.join(timeout=1.)
        result = capture.status()
        assert result["writer_stopped"] and result["state"] == "error"
        assert "clock" in result["error"].lower()
        assert result["failed_events"] >= 1 and not result["complete"]
        assert result["camera"]["writer_stopped"]
    finally:
        clock.now = 14.
        finish(capture)


@pytest.mark.parametrize("label", [None, True, "x" * 81, "first\nsecond", "hidden\x00suffix"])
def test_take_labels_are_bounded_text(label):
    with pytest.raises(ValueError):
        label_value(label)
