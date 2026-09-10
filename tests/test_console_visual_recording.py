import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from threading import Event
import time

from PIL import Image
import pytest

from argos.console.archive import ArchiveError
from argos.console.visual_recording import (
    MAX_FRAME_BYTES, MAX_HZ, MAX_ROW_BYTES, MAX_BYTES, VisualArchive,
    VisualArchiveError, VisualRecorder,
)


IDENTIFIER = "a" * 32
RUN = "b" * 32
VIDEO = "c" * 32
START = 20.
BINDING = {"started_at": START, "run_id": RUN}


def image(sequence=1, received=START, *, video_id=VIDEO, size=(32, 24), color="green"):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, "JPEG")
    return {"sequence": sequence, "received_at": received, "video_id": video_id,
            "width": size[0], "height": size[1], "jpeg": stream.getvalue()}


def vision(sequence=1, video_id=VIDEO):
    return {"frame_sequence": sequence, "video_id": video_id, "inference_ms": 12.,
            "model": "YOLOX-S", "variant": "s",
            "detections": [{"box": [.2, .1, .3, .7], "confidence": .91, "track_id": 4}]}


def capture(tmp_path, **options):
    recorder = VisualRecorder(tmp_path, **options)
    recorder.start(IDENTIFIER, START, run_id=RUN)
    return recorder


def finish(recorder, now=START + 1):
    recorder.stop(now)
    result = recorder.wait(3.)
    assert result["state"] == "complete", result
    return result


def recorded(tmp_path):
    recorder = capture(tmp_path)
    jpeg = image()
    assert recorder.append(START, frame=jpeg, vision=vision(), control={"phase": "armed", "owned": True})
    finish(recorder)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    return archive, meta, jpeg


def path(tmp_path):
    return tmp_path / f"{IDENTIFIER}.visual.sqlite3"


def change_meta(tmp_path, **changes):
    with sqlite3.connect(path(tmp_path)) as db:
        meta = json.loads(db.execute("SELECT metadata FROM recording").fetchone()[0])
        meta.update(changes)
        db.execute("UPDATE recording SET metadata=?", (json.dumps(meta),))


def query(archive, meta, offset):
    return archive.replay(IDENTIFIER, offset, revision=meta["revision"], **BINDING)


def test_durable_matching_jpeg_metadata_control_and_explicit_events(tmp_path):
    recorder = capture(tmp_path)
    frame = image()
    recorder.event(START, "claim", "Take control requested", status="accepted")
    control = {"phase": "armed", "owned": True, "axes": {"forward": .2},
               "framing": {"phase": "active", "paused": False, "target_id": 4, "reference_height": .3}}
    assert recorder.append(START + .1, frame=frame, vision=vision(), control=control)
    status = finish(recorder)
    assert (status["frames"], status["samples"], status["events"]) == (1, 1, 2)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    assert meta["state"] == "complete"
    assert meta["run_id"] == RUN and meta["duration_s"] == 1
    view = query(archive, meta, .2)
    assert view["sample"]["control"] == control
    assert view["frame"]["detections"] == vision()["detections"]
    assert view["frame"]["available_at_s"] == pytest.approx(.1)
    assert view["frame"]["at_s"] == 0
    assert [event["kind"] for event in view["events"]] == ["claim", "control_state"]
    assert archive.frame(IDENTIFIER, 0, revision=meta["revision"], **BINDING) == frame["jpeg"]
    assert archive.completed_path(IDENTIFIER, revision=meta["revision"], **BINDING) == path(tmp_path)
    assert VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)["revision"] == meta["revision"]


def test_rewind_never_uses_future_frame_or_analysis_result(tmp_path):
    recorder = capture(tmp_path)
    first = image(received=START + .05)
    recorder.append(START + .1, frame=first)
    recorder.append(START + .3, frame=first, vision=vision())
    recorder.append(START + .6, frame=image(2, START + .5, color="red"), vision=vision(2))
    finish(recorder)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    assert query(archive, meta, .7)["frame"]["index"] == 2
    assert query(archive, meta, .4)["frame"]["index"] == 1
    raw = query(archive, meta, .2)["frame"]
    assert raw["index"] == 0 and raw["vision"] is None and raw["detections"] == []
    before = query(archive, meta, .04)
    assert before["frame"] is None and before["sample"] is None and before["state"] == "waiting"


def test_source_sequence_reset_and_mismatched_boxes_do_not_cross_pair(tmp_path):
    recorder = capture(tmp_path)
    recorder.append(START, frame=image(), vision=vision())
    recorder.append(START + .2, frame=image(video_id="d" * 32), vision=vision())
    finish(recorder)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    assert meta["frames"] == 2
    second = query(archive, meta, .2)["frame"]
    assert second["source_sequence"] == 1 and second["video_id"] == "d" * 32
    assert second["detections"] == []


@pytest.mark.parametrize("duplicate", [False, True])
def test_matched_detection_ids_are_positive_and_unique(tmp_path, duplicate):
    recorder = capture(tmp_path)
    detection = vision()
    if duplicate:
        detection["detections"].append(dict(detection["detections"][0]))
    else:
        detection["detections"][0]["track_id"] = 0
    assert not recorder.append(START, frame=image(), vision=detection)
    assert recorder.wait(3.)["state"] == "error"


def test_rate_cap_and_identical_frame_reuse(tmp_path):
    recorder = capture(tmp_path)
    frame = image()
    assert recorder.append(START, frame=frame)
    assert not recorder.append(START + .01, frame=frame)
    assert not recorder.append(START + .05, frame=frame)
    assert recorder.append(START + 1 / MAX_HZ, frame=frame)
    status = finish(recorder)
    assert status["samples"] == 2 and status["frames"] == 1


def test_control_whitelist_does_not_store_tokens_requests_or_profiles(tmp_path):
    recorder = capture(tmp_path)
    recorder.append(START, control={"phase": "armed", "token": "secret-top", "body": "request-secret",
        "profile": {"token": "profile-secret"}, "command": {"name": "LAND", "token": "nested-secret"},
        "framing": {"phase": "active", "request": {"detail": "request-payload"}}})
    finish(recorder)
    data = path(tmp_path).read_bytes()
    for secret in (b"secret-top", b"request-secret", b"profile-secret", b"nested-secret", b"request-payload"):
        assert secret not in data
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    control = query(archive, meta, 0)["sample"]["control"]
    assert control == {"phase": "armed", "command": {"name": "LAND"}, "framing": {"phase": "active"}}


def test_slow_writer_and_stop_never_wait_on_disk_or_full_queue(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    original = VisualRecorder._connect_writer

    def slow(self, filename):
        entered.set()
        assert release.wait(2.)
        return original(self, filename)

    monkeypatch.setattr(VisualRecorder, "_connect_writer", slow)
    recorder = capture(tmp_path, queue_size=2)
    assert entered.wait(1.)
    before = time.monotonic()
    assert recorder.append(START, frame=image())
    assert recorder.append(START + .1, frame=image(2, START + .1))
    assert not recorder.append(START + .2, frame=image(3, START + .2))
    assert time.monotonic() - before < .1
    assert recorder.snapshot()["state"] == "finalizing"
    assert recorder.snapshot()["end_reason"] == "queue_overflow"
    assert recorder.snapshot()["dropped"] == 1
    recorder.stop(START + .3)
    with pytest.raises(ValueError, match="finalizing"):
        recorder.start("d" * 32, START + 2, run_id=RUN)
    release.set()
    status = recorder.wait(3.)
    assert status["state"] == "complete" and status["frames"] == 2
    assert status["dropped"] == 1


def test_abort_is_nonblocking_and_writer_discards_bounded_queue(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    original = VisualRecorder._connect_writer

    def slow(self, filename):
        entered.set()
        assert release.wait(2.)
        return original(self, filename)

    monkeypatch.setattr(VisualRecorder, "_connect_writer", slow)
    recorder = VisualRecorder(tmp_path, queue_size=2)
    assert recorder.abort()["state"] == "idle"
    recorder.start(IDENTIFIER, START, run_id=RUN)
    assert entered.wait(1.)
    assert recorder.append(START, frame=image())
    assert recorder.append(START + .1, frame=image(2, START + .1))
    before = time.monotonic()
    status = recorder.abort("Visual stop failed")
    assert time.monotonic() - before < .1
    assert status["state"] == "error" and status["detail"] == "Visual stop failed"
    assert not recorder.append(START + .2, frame=image())
    release.set()
    assert recorder.wait(3.)["state"] == "error"
    assert not recorder._thread.is_alive() and recorder._queue.empty()
    with pytest.raises(VisualArchiveError, match="complete"):
        VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)
    recorder.start("d" * 32, START + 1, run_id=RUN)
    recorder.stop(START + 2)
    assert recorder.wait(3.)["state"] == "complete"
    assert recorder.abort()["state"] == "complete"


@pytest.mark.parametrize("options,reason", [({"max_samples": 2}, "sample_limit"),
                                         ({"max_events": 2}, "event_limit")])
def test_sample_and_event_limits_finalize_cleanly(tmp_path, options, reason):
    recorder = capture(tmp_path, **options)
    recorder.append(START, frame=image(), control={"phase": "armed"})
    recorder.append(START + .1, frame=image(2, START + .1), control={"phase": "landing"})
    status = recorder.wait(3.)
    assert status["state"] == "complete" and status["end_reason"] == reason
    assert VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)["state"] == "complete"


def test_duration_limit_cannot_extend_end_to_late_receipt(tmp_path):
    recorder = capture(tmp_path, max_duration=.5)
    recorder.append(START, frame=image())
    assert not recorder.append(START + 20, frame=image(2, START + 20))
    status = recorder.wait(3.)
    assert status["state"] == "complete" and status["end_reason"] == "duration_limit"
    assert status["ended_at"] == START + .5


def test_size_limit_reserves_footer_and_does_not_grow_past_bound(tmp_path):
    recorder = capture(tmp_path, max_bytes=192 * 1024, queue_size=32)
    for index in range(20):
        recorder.append(START + index * .1, frame=image(index, START + index * .1))
    recorder.stop(START + 2)
    status = recorder.wait(3.)
    assert status["state"] == "complete" and status["end_reason"] == "size_limit"
    assert 0 < status["frames"] < 20 and status["dropped"] > 0
    assert path(tmp_path).stat().st_size <= 192 * 1024
    assert VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)["state"] == "complete"


def test_disk_failure_stays_optional_and_partial_file_is_not_replayable(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(VisualRecorder, "_write_sample", fail)
    recorder = capture(tmp_path)
    recorder.append(START, frame=image())
    assert recorder.wait(3.)["state"] == "error"
    assert not recorder.append(START + 1, frame=image())
    assert recorder.stop(START + 2)["state"] == "error"
    with pytest.raises(ArchiveError, match="complete"):
        VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)


def test_failed_directory_sync_never_commits_completion(tmp_path, monkeypatch):
    def fail_sync(fd):
        raise OSError("storage unavailable")

    monkeypatch.setattr(os, "fsync", fail_sync)
    recorder = capture(tmp_path)
    recorder.append(START, frame=image())
    recorder.stop(START + 1)
    assert recorder.wait(3.)["state"] == "error"
    with pytest.raises(VisualArchiveError, match="complete"):
        VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)


def test_writer_checks_decoded_jpeg_before_declaring_complete(tmp_path):
    recorder = capture(tmp_path)
    assert recorder.append(START, frame={**image(), "width": 33})
    assert recorder.wait(3.)["state"] == "error"
    with pytest.raises(VisualArchiveError, match="complete"):
        VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)


def test_disk_stat_does_not_hold_the_status_lock(tmp_path, monkeypatch):
    from threading import current_thread
    entered, release = Event(), Event()
    original = Path.stat

    def slow_stat(filename, *args, **kwargs):
        if filename.name.endswith(".visual.sqlite3") and current_thread().name == "argos-visual-recorder":
            entered.set()
            assert release.wait(2.)
        return original(filename, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", slow_stat)
    recorder = capture(tmp_path)
    recorder.append(START, frame=image())
    assert entered.wait(1.)
    before = time.monotonic()
    assert recorder.snapshot()["state"] == "recording"
    assert recorder.stop(START + 1)["state"] == "finalizing"
    assert time.monotonic() - before < .1
    release.set()
    assert recorder.wait(3.)["state"] == "complete"


def test_creation_error_and_idle_stop_are_harmless(tmp_path):
    recorder = VisualRecorder(tmp_path / "not-a-directory")
    assert recorder.stop(START)["state"] == "idle"
    (tmp_path / "not-a-directory").write_text("existing user file")
    recorder.start(IDENTIFIER, START, run_id=RUN)
    assert recorder.wait(3.)["state"] == "error"
    assert (tmp_path / "not-a-directory").read_text() == "existing user file"


def test_existing_identifier_is_never_overwritten(tmp_path):
    original = b"existing user file"
    path(tmp_path).write_bytes(original)
    recorder = capture(tmp_path)
    assert recorder.wait(3.)["state"] == "error"
    assert path(tmp_path).read_bytes() == original


@pytest.mark.parametrize("bad", [{"jpeg": b"not-jpeg"}, {"jpeg": b"\xff\xd8" + b"x" * MAX_FRAME_BYTES + b"\xff\xd9"},
                                  {"width": 9000}, {"received_at": float("nan")}, {"sequence": True}])
def test_invalid_frame_metadata_ends_only_optional_capture(tmp_path, bad):
    recorder = capture(tmp_path)
    assert not recorder.append(START, frame={**image(), **bad})
    assert recorder.snapshot()["state"] == "error"
    recorder.wait(3.)


def test_backwards_receipt_clock_rejected_and_known_recent_prestart_image_retained(tmp_path):
    recorder = capture(tmp_path)
    recorder.append(START, frame=image(received=START - .2))
    finish(recorder)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    initial = query(archive, meta, 0)["frame"]
    assert initial["at_s"] == pytest.approx(-.2) and initial["age_s"] == pytest.approx(.2)
    other = VisualRecorder(tmp_path)
    other.start("d" * 32, START, run_id=RUN)
    other.event(START + .1, "claim", "Take control")
    assert not other.append(START)
    assert other.wait(3.)["state"] == "error"


def test_missing_video_and_missing_legacy_sidecar_are_explicit(tmp_path):
    archive = VisualArchive(tmp_path)
    assert archive.metadata(IDENTIFIER, **BINDING)["state"] == "missing"
    recorder = capture(tmp_path)
    recorder.append(START, control={"phase": "idle"})
    finish(recorder)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    result = query(archive, meta, 0)
    assert result["frame"] is None and result["sample"]["control"]["phase"] == "idle"
    assert meta["frames"] == 0


def test_stale_gaps_and_cursor_after_media_end_never_appear_recent(tmp_path):
    recorder = capture(tmp_path)
    recorder.append(START, frame=image())
    recorder.append(START + 1.1, frame=image())
    finish(recorder, START + 1.2)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    assert query(archive, meta, .5)["state"] == "gap"
    assert query(archive, meta, .5)["frame"]["state"] == "stale"
    assert query(archive, meta, 1.1)["state"] == "stale"
    assert query(archive, meta, 2)["state"] == "ended"
    assert query(archive, meta, 2)["frame"]["state"] == "stale"


def test_event_window_is_bounded_counted_and_rewinds(tmp_path):
    recorder = capture(tmp_path, queue_size=32)
    for index in range(72):
        recorder.event(START + index * .01, "framing", f"Request {index}", status="accepted")
        if (index + 1) % 24 == 0:
            deadline = time.monotonic() + 2.
            while recorder.snapshot()["events"] < index + 1 and time.monotonic() < deadline:
                time.sleep(.005)
            assert recorder.snapshot()["events"] == index + 1
    finish(recorder)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    current = query(archive, meta, .9)
    assert current["events_count"] == 72 and len(current["events"]) == current["events_limit"] == 50
    assert current["events"][0]["index"] == 22 and current["events"][-1]["index"] == 71
    earlier = query(archive, meta, .105)
    assert earlier["events_count"] == len(earlier["events"]) == 11
    assert earlier["events"][-1]["index"] == 10


def test_actual_control_transitions_and_failure_evidence_remain_public(tmp_path):
    recorder = capture(tmp_path)
    state = {"at": START, "phase": "switching",
             "mode_transition": {"from_mode": 2, "to_mode": 0, "target_throttle": .61},
             "command": {"action": "switch_mode", "command_id": 176, "transport": "accepted",
                         "ack": 0, "observed": False, "state": "accepted"},
             "interruption": {"reason": "Lost target", "framing_loss": {"evidence_at": START,
                 "detections": vision()["detections"]}},
             "framing": {"phase": "active", "paused": True, "reference_height": .23}}
    recorder.append(START, control=state)
    finish(recorder)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    assert query(archive, meta, 0)["sample"]["control"] == state


def test_sampled_control_event_uses_readable_labels_without_changing_identity(tmp_path):
    recorder = capture(tmp_path)
    state = {"phase": "prepared", "owned": True, "selected_mode": 2,
             "vehicle": {"mode": 2, "armed": False}, "axes": {"yaw": 0.},
             "framing": {"phase": "selected", "target_id": 4, "reference_height": .23,
                         "reason": "Person selected"}}
    recorder.append(START, control=state)
    recorder.append(START + .1, control={**state, "axes": {"yaw": .2}})
    recorder.append(START + .2, control={**state, "vehicle": {"mode": 9, "armed": True},
        "framing": {**state["framing"], "reason": "Image framing paused. " * 30}})
    finish(recorder)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    events = query(archive, meta, .3)["events"]
    assert len(events) == 2  # Changed axes alone remain outside transition identity.
    assert events[0]["detail"] == (
        "Control prepared; operator owns control; mode AltHold; disarmed; "
        "framing selected; person #4; reference 23.0%; reason: Person selected")
    assert "observed mode Land; armed" in events[1]["detail"]
    assert len(events[1]["detail"]) == 400
    assert all(event["kind"] == "control_state" and event["status"] == "sampled" for event in events)


@pytest.mark.parametrize("changes", [{"started_at": START + .1}, {"run_id": "d" * 32}, {"id": "d" * 32}])
def test_sidecar_binding_cannot_pair_with_different_telemetry(tmp_path, changes):
    archive, meta, jpeg = recorded(tmp_path)
    change_meta(tmp_path, **changes)
    with pytest.raises(VisualArchiveError, match="different telemetry"):
        archive.metadata(IDENTIFIER, **BINDING)


@pytest.mark.parametrize("changes", [{"state": "recording"}, {"frames": 8}, {"version": 2},
                                     {"ended_at": float("nan")}, {"unknown": "field"}])
def test_incomplete_or_corrupt_metadata_is_refused(tmp_path, changes):
    archive, meta, jpeg = recorded(tmp_path)
    change_meta(tmp_path, **changes)
    with pytest.raises(VisualArchiveError):
        archive.metadata(IDENTIFIER, **BINDING)


def test_schema_is_exact_and_no_views_or_triggers_accepted(tmp_path):
    archive, meta, jpeg = recorded(tmp_path)
    with sqlite3.connect(path(tmp_path)) as db:
        db.execute("CREATE VIEW hidden AS SELECT jpeg FROM frames")
    with pytest.raises(VisualArchiveError, match="schema"):
        archive.metadata(IDENTIFIER, **BINDING)


def test_symlink_and_nonregular_inputs_refused_without_blocking(tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"unrelated")
    path(tmp_path).symlink_to(outside)
    with pytest.raises(VisualArchiveError):
        VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)
    path(tmp_path).unlink()
    os.mkfifo(path(tmp_path))
    before = time.monotonic()
    with pytest.raises(VisualArchiveError):
        VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)
    assert time.monotonic() - before < .2


def test_revision_change_rejects_old_frame_cursor_and_download(tmp_path):
    archive, meta, jpeg = recorded(tmp_path)
    change_meta(tmp_path, detail="Updated receipt")
    newer = archive.metadata(IDENTIFIER, **BINDING)
    assert newer["revision"] != meta["revision"]
    for operation in (lambda: query(archive, meta, 0),
                      lambda: archive.frame(IDENTIFIER, 0, revision=meta["revision"], **BINDING),
                      lambda: archive.open_download(IDENTIFIER, revision=meta["revision"], **BINDING)):
        with pytest.raises(VisualArchiveError, match="changed"):
            operation()


def test_missing_exact_frame_does_not_substitute_latest(tmp_path):
    archive, meta, jpeg = recorded(tmp_path)
    with pytest.raises(VisualArchiveError, match="not found"):
        archive.frame(IDENTIFIER, 12, revision=meta["revision"], **BINDING)


@pytest.mark.parametrize("mode", ["digest", "jpeg", "dimensions"])
def test_frame_digest_and_decoded_dimensions_are_checked(tmp_path, mode):
    archive, meta, frame = recorded(tmp_path)
    with sqlite3.connect(path(tmp_path)) as db:
        if mode == "digest":
            db.execute("UPDATE frames SET jpeg=?", (image(color="red")["jpeg"],))
        elif mode == "jpeg":
            broken = b"\xff\xd8not-a-jpeg\xff\xd9"
            db.execute("UPDATE frames SET jpeg=?,digest=?", (broken, hashlib.sha256(broken).hexdigest()))
        else:
            db.execute("UPDATE frames SET width=48")
    meta = archive.metadata(IDENTIFIER, **BINDING)
    with pytest.raises(VisualArchiveError):
        archive.frame(IDENTIFIER, 0, revision=meta["revision"], **BINDING)


def test_decoder_bomb_rejection_uses_archive_error_contract(tmp_path, monkeypatch):
    archive, meta, frame = recorded(tmp_path)

    def too_large(*args, **kwargs):
        raise Image.DecompressionBombError("oversized image")

    monkeypatch.setattr(Image, "open", too_large)
    with pytest.raises(VisualArchiveError, match="JPEG validation"):
        archive.frame(IDENTIFIER, 0, revision=meta["revision"], **BINDING)


def test_open_download_uses_verified_inode_after_path_replacement(tmp_path):
    archive, meta, frame = recorded(tmp_path)
    stream = archive.open_download(IDENTIFIER, revision=meta["revision"], **BINDING)
    before = os.fstat(stream.fileno())
    assert stream.visual_signature == (before.st_dev, before.st_ino, before.st_size,
                                        before.st_mtime_ns, before.st_ctime_ns)
    assert stream.visual_size_bytes == before.st_size
    expected = path(tmp_path).read_bytes()
    path(tmp_path).rename(tmp_path / "original.sqlite3")
    path(tmp_path).write_bytes(b"replacement")
    try:
        assert stream.read() == expected
    finally:
        stream.close()
    with pytest.raises(VisualArchiveError):
        archive.open_download(IDENTIFIER, revision=meta["revision"], **BINDING)


def test_archive_query_never_changes_database(tmp_path):
    archive, meta, frame = recorded(tmp_path)
    before = path(tmp_path).stat()
    digest = hashlib.sha256(path(tmp_path).read_bytes()).hexdigest()
    query(archive, meta, .2)
    archive.frame(IDENTIFIER, 0, revision=meta["revision"], **BINDING)
    assert path(tmp_path).stat().st_mtime_ns == before.st_mtime_ns
    assert hashlib.sha256(path(tmp_path).read_bytes()).hexdigest() == digest
    assert not list(tmp_path.glob("*-journal")) and not list(tmp_path.glob("*-wal"))


def test_future_frame_reference_and_unknown_control_fields_refused(tmp_path):
    recorder = capture(tmp_path)
    recorder.append(START, frame=image())
    recorder.append(START + .2, frame=image(2, START + .2))
    finish(recorder)
    with sqlite3.connect(path(tmp_path)) as db:
        db.execute("UPDATE samples SET frame_idx=1 WHERE idx=0")
    with pytest.raises(VisualArchiveError, match="future"):
        VisualArchive(tmp_path).metadata(IDENTIFIER, **BINDING)


def test_public_snapshot_is_detached_from_later_caller_mutation(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    original = VisualRecorder._connect_writer

    def wait_writer(self, filename):
        entered.set()
        assert release.wait(2.)
        return original(self, filename)

    monkeypatch.setattr(VisualRecorder, "_connect_writer", wait_writer)
    recorder = capture(tmp_path)
    assert entered.wait(1.)
    data = {"phase": "armed", "axes": {"yaw": .2}}
    recorder.append(START, control=data)
    data["axes"]["yaw"] = .9
    recorder.stop(START + 1)
    release.set()
    assert recorder.wait(3.)["state"] == "complete"
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    assert query(archive, meta, 0)["sample"]["control"]["axes"]["yaw"] == .2


@pytest.mark.parametrize("options", [{"queue_size": 100}, {"max_bytes": MAX_BYTES + 1},
                                     {"max_events": True}, {"max_duration": float("inf")}])
def test_resource_limits_cannot_be_relaxed_by_constructor(options, tmp_path):
    with pytest.raises(ValueError):
        VisualRecorder(tmp_path, **options)


@pytest.mark.parametrize("profile", ["full", "pilot_throttle"])
def test_recorded_framing_profile_survives_replay_and_loss_without_arbitrary_data(tmp_path, profile):
    recorder = capture(tmp_path)
    control = {"phase": "armed", "throttle": .463,
               "framing": {"phase": "active", "profile": profile,
                           "profiles": {"pilot_throttle": {"secret": "not-public"}},
                           "last_loss": {"profile": profile, "reason": "Target lost"}},
               "profile": {"secret": "not-public"}}
    recorder.append(START + .1, frame=image(), control=control)
    finish(recorder)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    replay = query(archive, meta, .2)
    saved = replay["sample"]["control"]
    assert saved == {"phase": "armed", "throttle": .463,
                     "framing": {"phase": "active", "profile": profile,
                                 "last_loss": {"profile": profile, "reason": "Target lost"}}}
    label = "pilot throttle" if profile == "pilot_throttle" else "full framing"
    assert label in replay["events"][0]["detail"]
    assert b"not-public" not in path(tmp_path).read_bytes()


@pytest.mark.parametrize("value", [{"reason": "private-profile"}, "unknown-profile", ["full"]])
def test_arbitrary_nested_profile_is_not_archived(tmp_path, value):
    recorder = capture(tmp_path)
    recorder.append(START + .1, frame=image(), control={"framing": {"phase": "idle", "profile": value}})
    finish(recorder)
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    assert query(archive, meta, .2)["sample"]["control"] == {"framing": {"phase": "idle"}}
