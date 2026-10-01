"""Full-cadence camera archives, queue pressure and disk failure isolation."""

from dataclasses import replace
import io
import json
from threading import Event, get_ident
import time

import pytest

from argos.console.camera_recording import CameraRecorder, INDEX_NAME, MANIFEST_NAME, MEDIA_NAME
from argos.console.video import CameraFrame, VideoSample, VideoStore


def frame(sequence=1, at=10., *, source_id="camera-one", jpeg=b"\xff\xd8original\xff\xd9"):
    return CameraFrame(VideoSample(jpeg, sequence, at), 2, 2,
                       "device", "/dev/video2", None, source_id)


def rows(path):
    return [json.loads(line) for line in (path / INDEX_NAME).read_text().splitlines()]


class PausedRecorder(CameraRecorder):
    """Allow deterministic admission while the IO worker has not started."""

    def __init__(self, *args, **kwargs):
        self.begin = Event()
        super().__init__(*args, **kwargs)

    def _run(self):
        assert self.begin.wait(3.)
        super()._run()


class BlockedWriter(CameraRecorder):
    def __init__(self, *args, **kwargs):
        self.writing = Event()
        self.release = Event()
        self.writer_thread = None
        super().__init__(*args, **kwargs)

    def _write_frame(self, value):
        self.writer_thread = get_ident()
        self.writing.set()
        assert self.release.wait(3.)
        return super()._write_frame(value)


def make(path, cls=PausedRecorder, **kwargs):
    return cls(path, session_id="battery-01", started_at=10., clock=lambda: 14., **kwargs)


def finish(recorder, at=14.):
    if isinstance(recorder, PausedRecorder):
        recorder.begin.set()
    return recorder.stop(at)


def test_every_received_image_is_archived_above_visual_ten_hz_with_unchanged_jpeg(tmp_path):
    image = pytest.importorskip("PIL.Image")
    recorder = make(tmp_path)
    store = VideoStore(source="device", endpoint="/dev/video2")
    store.add_frame_sink(recorder.submit)
    originals = []
    for i in range(90):
        received_at = 10. + i / 30.
        assert store.accept_raw(width=2, height=2, step=6, pixel_format="RGB_INT8",
            data=bytes([i, 0, 0]) * 4, received_at=received_at)
        originals.append(store.latest(received_at).jpeg)
    result = finish(recorder)
    assert result["complete"] and result["written_frames"] == 90
    assert result["observed_fps"] == pytest.approx(30.)
    assert result["dropped_frames"] == 0
    payload = (tmp_path / MEDIA_NAME).read_bytes()
    assert payload == b"".join(originals)
    index = rows(tmp_path)
    assert len(index) == 90
    offset = 0
    for i, row in enumerate(index):
        assert row["sequence"] == i + 1
        assert row["received_at"] == 10. + i / 30.
        assert row["elapsed_s"] == row["received_at"] - 10.
        assert row["source_id"] == store.source_id
        assert row["offset"] == offset
        jpeg = payload[offset:offset + row["size_bytes"]]
        assert jpeg == originals[i]
        assert image.open(io.BytesIO(jpeg)).size == (2, 2)
        offset += row["size_bytes"]
    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text())
    assert manifest["complete"] and manifest["writer_stopped"]
    assert manifest["size_bytes"] == len(payload)
    assert manifest["index_bytes"] == (tmp_path / INDEX_NAME).stat().st_size


@pytest.mark.parametrize("bound", [{"max_queue_frames": 1}, {"max_queue_bytes": 13}])
def test_slow_disk_drops_explicitly_without_blocking_capture_and_counts_inflight(tmp_path, bound):
    recorder = make(tmp_path, BlockedWriter, **bound)
    try:
        assert recorder.submit(frame())
        assert recorder.writing.wait(1.)
        before = time.monotonic()
        assert not recorder.submit(frame(2, 10.1))
        assert time.monotonic() - before < .2
        status = recorder.status()
        assert status["buffered_frames"] == 1
        assert status["buffered_bytes"] == len(frame().sample.jpeg)
        assert status["dropped_queue"] == 1
        assert recorder.writer_thread != get_ident()
    finally:
        recorder.release.set()
        status = recorder.stop(11.)
    assert status["state"] == "complete" and not status["complete"]
    assert status["written_frames"] == 1
    assert status["dropped_frames"] == 1


def test_queue_mutex_contention_is_counted_without_waiting(tmp_path):
    recorder = make(tmp_path)
    with recorder._lock:
        assert not recorder.submit(frame())
    assert finish(recorder)["dropped_contention"] == 1


@pytest.mark.parametrize("bound,reason", [
    ({"max_bytes": len(frame().sample.jpeg)}, "size_limit"),
    ({"max_frames": 1}, "frame_limit"),
    ({"max_duration_s": .1}, "duration_limit"),
])
def test_capture_bounds_stop_admission_and_drain_prior_frames(tmp_path, bound, reason):
    recorder = make(tmp_path, **bound)
    assert recorder.submit(frame())
    assert not recorder.submit(frame(2, 10.2))
    assert not recorder.submit(frame(3, 10.3))
    status = finish(recorder)
    assert status["reason"] == reason
    assert status["state"] == "complete" and not status["complete"]
    assert status["written_frames"] == 1 and status["dropped_limit"] == 1
    assert (tmp_path / MEDIA_NAME).read_bytes() == frame().sample.jpeg


def test_source_reopen_and_dimensions_preserve_per_frame_provenance(tmp_path):
    recorder = make(tmp_path)
    first = frame(20, 10.)
    second = replace(frame(1, 10.5, source_id="camera-two"), width=4,
                     source_stamp=(7000, 8), source="gazebo", endpoint="/camera/image")
    assert recorder.submit(first)
    assert recorder.submit(second)
    status = finish(recorder)
    assert status["complete"] and len(status["sources"]) == 2
    first_row, second_row = rows(tmp_path)
    assert first_row["sequence"] == 20 and second_row["sequence"] == 1
    assert second_row["source_stamp"] == {"sec": 7000, "nsec": 8}
    assert second_row["received_at"] == 10.5 and second_row["width"] == 4


def test_invalid_or_backwards_frames_are_explicit_and_do_not_break_writer(tmp_path):
    recorder = make(tmp_path)
    assert not recorder.submit(None)
    assert not recorder.submit(frame(jpeg=b"broken"))
    assert not recorder.submit(frame(at=9.))
    assert recorder.submit(frame(at=11.))
    assert not recorder.submit(frame(2, 10.9))
    result = finish(recorder)
    assert result["written_frames"] == 1 and result["dropped_invalid"] == 4
    assert not result["complete"]


def test_stop_timeout_exposes_writer_ownership_then_finishes_and_rejects_late_frames(tmp_path):
    recorder = make(tmp_path, BlockedWriter)
    try:
        assert recorder.submit(frame())
        assert recorder.writing.wait(1.)
        status = recorder.stop(11., timeout=.01)
        assert status["state"] == "finalizing" and status["stop_timed_out"]
        assert not status["writer_stopped"]
        assert not recorder.submit(frame(2, 11.))
    finally:
        recorder.release.set()
        status = recorder.stop(11.)
    assert status["complete"] and status["writer_stopped"]
    assert not status["stop_timed_out"]
    assert recorder.stop(12.)["ended_at"] == 11.


def test_stop_time_cannot_precede_an_image_admitted_by_competing_capture(tmp_path):
    recorder = make(tmp_path)
    assert recorder.submit(frame(at=11.001))
    result = finish(recorder, at=11.)
    assert result["complete"]
    assert result["ended_at"] == 11.001


def test_partial_disk_failure_removes_unindexed_tail_and_reports_discarded_frames(tmp_path):
    class BrokenWriter(PausedRecorder):
        def _write_frame(self, value):
            self._media.write(value.sample.jpeg[:5])
            raise OSError("disk full")

    recorder = make(tmp_path, BrokenWriter)
    assert recorder.submit(frame())
    assert recorder.submit(frame(2, 10.1))
    result = finish(recorder)
    assert result["state"] == "error" and not result["complete"]
    assert "disk full" in result["error"]
    assert result["discarded_error"] == 2 and result["written_frames"] == 0
    assert (tmp_path / MEDIA_NAME).stat().st_size == 0
    assert rows(tmp_path) == []
    assert json.loads((tmp_path / MANIFEST_NAME).read_text())["state"] == "error"
    assert not recorder.submit(frame(3, 11.))


def test_no_camera_frames_expire_without_any_producer_call(tmp_path):
    recorder = CameraRecorder(tmp_path, session_id="empty", started_at=time.monotonic(),
                              max_duration_s=.02)
    assert recorder._finished.wait(1.)
    result = recorder.status()
    assert result["reason"] == "duration_limit"
    assert result["written_frames"] == 0 and result["empty_capture"]
    assert not result["complete"] and result["elapsed_s"] == pytest.approx(.02)


def test_exclusive_start_never_overwrites_existing_recording_or_removes_it(tmp_path):
    existing = tmp_path / INDEX_NAME
    existing.write_bytes(b"prior recording")
    with pytest.raises(FileExistsError):
        make(tmp_path)
    assert existing.read_bytes() == b"prior recording"
    assert not (tmp_path / MEDIA_NAME).exists()
    assert not (tmp_path / MANIFEST_NAME).exists()


@pytest.mark.parametrize("kwargs", [
    {"max_bytes": 0}, {"max_frames": True}, {"max_queue_bytes": 0},
    {"max_queue_frames": 129}, {"max_duration_s": float("inf")},
    {"max_duration_s": 3601},
])
def test_invalid_limits_do_not_create_archive(tmp_path, kwargs):
    with pytest.raises(ValueError):
        make(tmp_path, **kwargs)
    assert list(tmp_path.iterdir()) == []
