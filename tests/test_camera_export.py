import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess

from PIL import Image
import pytest

from argos import camera_export as export


def archive(tmp_path, *, times=(100.123456, 100.200003, 101.700007), sequences=None, drops=0):
    directory = tmp_path / "recording"
    directory.mkdir()
    images, rows, offset = [], [], 0
    for number, received in enumerate(times):
        image = io.BytesIO()
        Image.new("RGB", (16, 12), (number * 70, 30, 180)).save(image, "JPEG")
        jpeg = image.getvalue()
        images.append(jpeg)
        rows.append({"schema": 1, "session_id": "test-session", "frame": number,
                     "sequence": sequences[number] if sequences else number + 1,
                     "received_at": received, "elapsed_s": received - 100.,
                     "source": "device", "endpoint": "/dev/video2", "source_id": "source-a",
                     "width": 16, "height": 12, "source_stamp": None,
                     "offset": offset, "size_bytes": len(jpeg)})
        offset += len(jpeg)
    (directory / export.MEDIA_NAME).write_bytes(b"".join(images))
    write_rows(directory, rows)
    manifest = {"schema": 1, "session_id": "test-session", "state": "complete", "writer_stopped": True,
                "format": "mjpeg_with_receive_timestamp_index", "started_at": 100., "ended_at": 102.,
                "error": "", "discarded_error": 0, "accepted_frames": len(rows), "written_frames": len(rows),
                "buffered_frames": 0, "buffered_bytes": 0, "size_bytes": offset,
                "index_bytes": (directory / export.INDEX_NAME).stat().st_size,
                "media": export.MEDIA_NAME, "index": export.INDEX_NAME,
                "dropped_queue": drops, "dropped_contention": 0, "dropped_invalid": 0, "dropped_limit": 0,
                "dropped_frames": drops, "complete": drops == 0,
                "first_received_at": times[0], "last_received_at": times[-1]}
    (directory / export.MANIFEST_NAME).write_text(json.dumps(manifest))
    return directory, rows, manifest, images


def write_rows(directory, rows):
    (directory / export.INDEX_NAME).write_text("".join(json.dumps(row) + "\n" for row in rows))


def rewrite(directory, rows, manifest):
    write_rows(directory, rows)
    manifest["index_bytes"] = (directory / export.INDEX_NAME).stat().st_size
    (directory / export.MANIFEST_NAME).write_text(json.dumps(manifest))


def extract(directory, tmp_path, **kwargs):
    destination = tmp_path / "extracted"
    destination.mkdir(exist_ok=True)
    return export.extract_archive(directory, destination, **kwargs)


def test_extract_preserves_jpeg_bytes_original_times_and_marks_gap(tmp_path):
    directory, rows, _, images = archive(tmp_path, sequences=(11, 12, 15))
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    result = extract(directory, tmp_path)
    assert [frame["received_at"] for frame in result["frames"]] == [row["received_at"] for row in rows]
    assert [frame["video_pts_us"] for frame in result["frames"]] == [0, 76547, 1576551]
    assert result["gaps"][0]["missing_sequences"] == 2
    assert result["gaps"][0]["from_elapsed_s"] == rows[1]["elapsed_s"]
    for frame, jpeg in zip(result["frames"], images):
        assert (tmp_path / "extracted" / frame["extracted_file"]).read_bytes() == jpeg
        assert frame["jpeg_sha256"] == hashlib.sha256(jpeg).hexdigest()
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.parametrize("mutation, message", [
    (lambda rows, data: data.update(state="recording"), "not finalized"),
    (lambda rows, data: data.update(writer_stopped=False), "not finalized"),
    (lambda rows, data: data.update(error="disk full"), "writer error"),
    (lambda rows, data: data.update(accepted_frames=4), "accepted_frames"),
    (lambda rows, data: data.update(schema=True), "manifest schema"),
    (lambda rows, data: rows[1].update(offset=1), "byte offset"),
    (lambda rows, data: rows[1].update(session_id="other"), "frame session"),
    (lambda rows, data: rows[1].update(elapsed_s=50), "inconsistent"),
    (lambda rows, data: rows[1].update(received_at=float("nan")), "Nonfinite"),
    (lambda rows, data: rows[1].update(source_id="new-camera"), "Source or dimensions"),
    (lambda rows, data: rows[0].update(width=15), "dimensions"),
    (lambda rows, data: rows[1].update(sequence=1), "must increase"),
])
def test_refuse_invalid_or_unfinalized_archives(tmp_path, mutation, message):
    directory, rows, manifest, _ = archive(tmp_path)
    mutation(rows, manifest)
    rewrite(directory, rows, manifest)
    with pytest.raises(ValueError, match=message):
        extract(directory, tmp_path)


def test_reject_duplicate_receive_times_without_silently_dropping_frames(tmp_path):
    directory, _, _, _ = archive(tmp_path, times=(100.1, 100.1, 100.2))
    with pytest.raises(ValueError, match="microsecond"):
        extract(directory, tmp_path)


def test_explicit_loss_policy_keeps_counters_and_indexed_frames(tmp_path):
    directory, _, _, _ = archive(tmp_path, drops=3)
    with pytest.raises(ValueError, match="allow-drops"):
        extract(directory, tmp_path)
    result = extract(directory, tmp_path, allow_drops=True)
    assert len(result["frames"]) == 3
    assert result["manifest"]["dropped_frames"] == 3
    assert result["manifest"]["complete"] is False


@pytest.mark.parametrize("reason", ["duration_limit", "size_limit", "frame_limit"])
def test_capture_limit_without_missing_frame_still_requires_explicit_policy(tmp_path, reason):
    directory, rows, manifest, _ = archive(tmp_path)
    manifest.update(reason=reason, complete=False)
    rewrite(directory, rows, manifest)
    with pytest.raises(ValueError, match="capture limit"):
        extract(directory, tmp_path)
    result = extract(directory, tmp_path, allow_drops=True)
    assert result["manifest"]["dropped_frames"] == 0
    assert result["manifest"]["reason"] == reason
    assert result["manifest"]["complete"] is False


@pytest.mark.parametrize("damage", ["truncated", "extra", "jpeg", "index_line", "symlink"])
def test_corrupt_or_redirected_input_is_rejected(tmp_path, damage):
    directory, rows, manifest, _ = archive(tmp_path)
    media = directory / export.MEDIA_NAME
    if damage == "truncated":
        media.write_bytes(media.read_bytes()[:-1])
    elif damage == "extra":
        media.write_bytes(media.read_bytes() + b"trailing")
    elif damage == "jpeg":
        value = bytearray(media.read_bytes())
        value[0] = 0
        media.write_bytes(value)
    elif damage == "index_line":
        index = directory / export.INDEX_NAME
        index.write_bytes(b"x" * (export.MAX_INDEX_LINE_BYTES + 1))
        manifest["index_bytes"] = index.stat().st_size
        (directory / export.MANIFEST_NAME).write_text(json.dumps(manifest))
    else:
        moved = tmp_path / "other.mjpeg"
        media.rename(moved)
        media.symlink_to(moved)
    with pytest.raises(ValueError):
        extract(directory, tmp_path)


def test_concat_uses_absolute_rounding_and_one_entry_per_image(tmp_path):
    directory, _, _, _ = archive(tmp_path)
    result = extract(directory, tmp_path)
    path = tmp_path / "frames.ffconcat"
    export.write_concat(result["frames"], path)
    text = path.read_text()
    assert text.count("\nfile ") == 3
    assert "duration 0.076547" in text
    assert "duration 1.500004" in text
    assert text.endswith("duration 0.000001\n")


def test_command_is_vfr_and_does_not_enable_overwrite_or_shell(tmp_path, monkeypatch):
    command = export.ffmpeg_command("/ffmpeg", tmp_path / "a.ffconcat", tmp_path / "b.mp4")
    assert "-n" in command and "-y" not in command
    assert command[command.index("-fps_mode") + 1] == "vfr"
    assert "1:1000000" in command
    def run(args, **kwargs):
        assert args == command
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
    monkeypatch.setattr(export.subprocess, "run", run)
    export._run(command, tmp_path, 10)


def test_output_protection_precedes_encoder(tmp_path, monkeypatch):
    directory, _, _, _ = archive(tmp_path)
    monkeypatch.setattr(export.shutil, "which", lambda _: pytest.fail("must reject before encoder lookup"))
    with pytest.raises(ValueError, match="outside"):
        export.export_camera(directory, directory / "result.mp4")
    output = tmp_path / "movie.mp4"
    sidecar = tmp_path / "movie.mp4.json"
    sidecar.write_text("keep me")
    with pytest.raises(ValueError, match="already exists"):
        export.export_camera(directory, output)
    assert sidecar.read_text() == "keep me"
    assert not output.exists()


def fake_encoder(monkeypatch):
    monkeypatch.setattr(export.shutil, "which", lambda _: "/test/ffmpeg")
    monkeypatch.setattr(export, "_run", lambda command, *_: Path(command[-1]).write_bytes(b"encoded test output"))
    monkeypatch.setattr(export, "verify_video", lambda _, video, frames, *args: frames[-1]["video_pts_us"] + 1)


def test_success_publishes_video_and_complete_provenance(tmp_path, monkeypatch):
    directory, rows, _, _ = archive(tmp_path)
    fake_encoder(monkeypatch)
    output = tmp_path / "result.mp4"
    result = export.export_camera(directory, output)
    saved = json.loads((tmp_path / "result.mp4.json").read_text())
    assert saved == result
    assert saved["source_offset_s"] == rows[0]["elapsed_s"]
    assert saved["video_duration_s"] == 1.576552
    assert saved["frames"][1]["received_at"] == rows[1]["received_at"]
    assert saved["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert not list(tmp_path.glob(".argos-camera-export-*"))


def test_modified_source_during_encode_prevents_publication(tmp_path, monkeypatch):
    directory, _, _, _ = archive(tmp_path)
    fake_encoder(monkeypatch)
    def mutate(command, *_):
        Path(command[-1]).write_bytes(b"encoded")
        with (directory / export.MEDIA_NAME).open("ab") as stream:
            stream.write(b"change")
    monkeypatch.setattr(export, "_run", mutate)
    with pytest.raises(ValueError, match="unchanged source"):
        export.export_camera(directory, tmp_path / "result.mp4")
    assert not (tmp_path / "result.mp4").exists()
    assert not (tmp_path / "result.mp4.json").exists()


def test_raced_in_sidecar_is_preserved_and_video_rolled_back(tmp_path, monkeypatch):
    directory, _, _, _ = archive(tmp_path)
    fake_encoder(monkeypatch)
    original = os.link
    def link(source, target):
        if str(target).endswith(".json"):
            Path(target).write_text("another export")
        original(source, target)
    monkeypatch.setattr(export.os, "link", link)
    with pytest.raises(FileExistsError):
        export.export_camera(directory, tmp_path / "result.mp4")
    assert not (tmp_path / "result.mp4").exists()
    assert (tmp_path / "result.mp4.json").read_text() == "another export"


@pytest.mark.parametrize("times", [(100.123456, 100.200003, 101.700007), (100.123456,)])
def test_real_ffmpeg_preserves_long_gap_all_pts_and_terminal_frame(tmp_path, times):
    executable = os.environ.get("ARGOS_TEST_FFMPEG") or shutil.which("ffmpeg")
    if not executable:
        pytest.skip("optional FFmpeg executable is not installed")
    directory, rows, _, _ = archive(tmp_path, times=times)
    output = tmp_path / "real.mp4"
    result = export.export_camera(directory, output, ffmpeg=executable)
    assert output.stat().st_size > 0
    assert result["frame_count"] == len(times)
    assert result["source_span_s"] == pytest.approx(times[-1] - times[0])
    assert result["video_duration_s"] >= result["source_span_s"]
    assert result["last_frame_duration_s"] > 0
    if len(times) > 1:
        assert result["gaps"][0]["interval_s"] == pytest.approx(1.500004)
    assert [frame["received_at"] for frame in result["frames"]] == [row["received_at"] for row in rows]


def test_actual_recorder_archive_is_accepted(tmp_path):
    from threading import Event
    from argos.console.camera_recording import CameraRecorder
    from argos.console.video import CameraFrame, VideoSample

    directory = tmp_path / "actual-camera"
    directory.mkdir()
    jpeg = io.BytesIO()
    Image.new("RGB", (16, 12)).save(jpeg, "JPEG")
    recorder = CameraRecorder(directory, session_id="actual", started_at=10., clock=lambda: 11.)
    try:
        sample = VideoSample(sequence=1, received_at=10.5, jpeg=jpeg.getvalue())
        frame = CameraFrame(sample=sample, source="device", endpoint="/dev/video2",
                            source_id="actual-source", width=16, height=12, source_stamp=None)
        # Admission can legitimately lose a startup lock race; retry without
        # assuming that an asynchronous writer must be idle at this instant.
        for _ in range(50):
            if recorder.submit(frame):
                break
            Event().wait(.002)
        else:
            pytest.fail("Camera writer never admitted the test image")
    finally:
        result = recorder.stop(11.)
    assert result["writer_stopped"]
    result = extract(directory, tmp_path, allow_drops=True)
    assert result["manifest"]["session_id"] == "actual"
    assert result["frames"][0]["video_pts_us"] == 0
