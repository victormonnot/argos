"""Exact archived pixels/source times for offline use, without acquisition IO."""
import hashlib
import sqlite3

from PIL import JpegImagePlugin
import pytest

from argos.console import visual_recording
from argos.console.visual_recording import MAX_SAMPLES, VisualArchive, VisualArchiveError
from test_console_visual_recording import (
    BINDING, IDENTIFIER, RUN, START, VIDEO, capture, change_meta, finish,
    image, path, recorded, vision,
)


def read(archive, meta, index=0, **binding):
    return archive.frame_record(IDENTIFIER, index, revision=meta["revision"],
                                **(binding or BINDING))


def test_exact_entries_keep_recorded_times_and_source_resets_without_telemetry(tmp_path):
    recorder = capture(tmp_path)
    first = image(sequence=91, received=START - .1)
    second_source = image(sequence=1, received=START + .55, video_id="d" * 32,
                          size=(16, 12), color="red")
    assert recorder.append(START + .1, frame=first)
    # The analysis became available later for the same source JPEG. Both
    # archived entries must remain addressable; the evaluator chooses deduping.
    assert recorder.append(START + .3, frame=first, vision=vision(sequence=91))
    assert recorder.append(START + .6, frame=second_source)
    finish(recorder)
    assert not list(tmp_path.glob("*.jsonl"))
    archive = VisualArchive(tmp_path)
    meta = archive.metadata(IDENTIFIER, **BINDING)
    assert meta["frames"] == 3
    before = path(tmp_path).stat()
    digest = hashlib.sha256(path(tmp_path).read_bytes()).hexdigest()

    last = read(archive, meta, 2)
    assert last == {"index": 2, "received_at": START + .55, "available_at": START + .6,
                    "sequence": 1, "video_id": "d" * 32, "width": 16, "height": 12,
                    "jpeg": second_source["jpeg"]}
    for index, available in ((0, START + .1), (1, START + .3)):
        earlier = read(archive, meta, index)
        assert earlier == {"index": index, "received_at": START - .1, "available_at": available,
                           "sequence": 91, "video_id": VIDEO, "width": 32, "height": 24,
                           "jpeg": first["jpeg"]}
        assert archive.frame(IDENTIFIER, index, revision=meta["revision"], **BINDING) == earlier["jpeg"]
    last["received_at"] = -1
    assert read(archive, meta, 2)["received_at"] == START + .55
    assert path(tmp_path).stat().st_mtime_ns == before.st_mtime_ns
    assert hashlib.sha256(path(tmp_path).read_bytes()).hexdigest() == digest
    assert not list(tmp_path.glob("*-journal")) and not list(tmp_path.glob("*-wal"))


@pytest.mark.parametrize("index", [-1, MAX_SAMPLES, 1.0, True, "0", None])
def test_invalid_or_missing_exact_index_is_never_a_cursor_lookup(tmp_path, index):
    archive, meta, _ = recorded(tmp_path)
    with pytest.raises(VisualArchiveError, match="index") as caught:
        read(archive, meta, index)
    assert caught.value.status == 404


def test_absent_exact_index_and_missing_sidecar_are_404(tmp_path):
    archive, meta, _ = recorded(tmp_path)
    with pytest.raises(VisualArchiveError, match="not found") as absent_index:
        read(archive, meta, 12)
    assert absent_index.value.status == 404
    path(tmp_path).unlink()
    with pytest.raises(VisualArchiveError, match="not found") as absent_file:
        read(archive, meta)
    assert absent_file.value.status == 404


@pytest.mark.parametrize("revision", [None, "", "a" * 63, "g" * 64])
def test_frame_record_requires_an_explicit_valid_revision(tmp_path, revision):
    archive, _, _ = recorded(tmp_path)
    with pytest.raises(VisualArchiveError, match="revision") as caught:
        read(archive, {"revision": revision})
    assert caught.value.status == 409


@pytest.mark.parametrize("binding", [
    {"started_at": START + .1, "run_id": RUN},
    {"started_at": START, "run_id": "e" * 32},
])
def test_warm_validation_cache_cannot_cross_recording_bindings(tmp_path, binding):
    archive, meta, _ = recorded(tmp_path)
    read(archive, meta)
    with pytest.raises(VisualArchiveError, match="different telemetry") as caught:
        read(archive, meta, **binding)
    assert caught.value.status == 409


def test_mutated_archive_requires_reopening_even_when_requested_pixels_unchanged(tmp_path):
    archive, meta, _ = recorded(tmp_path)
    change_meta(tmp_path, detail="A changed archive")
    with pytest.raises(VisualArchiveError, match="changed") as caught:
        read(archive, meta)
    assert caught.value.status == 409
    newer = archive.metadata(IDENTIFIER, **BINDING)
    assert newer["revision"] != meta["revision"]
    assert read(archive, newer)["index"] == 0


@pytest.mark.parametrize("mutation", [
    "UPDATE frames SET idx=9",
    "UPDATE frames SET received=available+1",
    "UPDATE frames SET video_id='not-a-video-id'",
    "UPDATE samples SET frame_idx=9",
])
def test_frame_record_reuses_whole_archive_index_and_source_validation(tmp_path, mutation):
    archive, meta, _ = recorded(tmp_path)
    with sqlite3.connect(path(tmp_path)) as db:
        db.execute(mutation)
    with pytest.raises(VisualArchiveError):
        read(archive, meta)


@pytest.mark.parametrize("mode", ["digest", "jpeg", "dimensions"])
def test_current_revision_still_requires_digest_and_decoded_jpeg_consistency(tmp_path, mode):
    archive, _, _ = recorded(tmp_path)
    with sqlite3.connect(path(tmp_path)) as db:
        if mode == "digest":
            db.execute("UPDATE frames SET jpeg=?", (image(color="red")["jpeg"],))
        elif mode == "jpeg":
            broken = b"\xff\xd8invalid-image\xff\xd9"
            db.execute("UPDATE frames SET jpeg=?,digest=?", (broken, hashlib.sha256(broken).hexdigest()))
        else:
            db.execute("UPDATE frames SET width=48")
    meta = archive.metadata(IDENTIFIER, **BINDING)
    with pytest.raises(VisualArchiveError, match="digest check|JPEG validation"):
        read(archive, meta)


def test_full_decode_failure_is_not_accepted_from_valid_jpeg_headers(tmp_path, monkeypatch):
    archive, meta, _ = recorded(tmp_path)

    def damaged_scan(self, *args, **kwargs):
        raise OSError("JPEG scan could not be fully decoded")

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", damaged_scan)
    with pytest.raises(VisualArchiveError, match="JPEG validation"):
        read(archive, meta)


def test_path_replaced_during_decode_cannot_return_old_pixels_as_new_archive(tmp_path, monkeypatch):
    archive, meta, _ = recorded(tmp_path)
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(path(tmp_path).read_bytes())
    original = visual_recording._check_jpeg

    def replace_after_decode(jpeg, width, height):
        original(jpeg, width, height)
        replacement.replace(path(tmp_path))

    monkeypatch.setattr(visual_recording, "_check_jpeg", replace_after_decode)
    with pytest.raises(VisualArchiveError, match="changed") as caught:
        read(archive, meta)
    assert caught.value.status == 409
