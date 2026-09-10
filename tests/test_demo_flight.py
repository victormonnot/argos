"""The public visitor fixture must stay intact and replay recorded evidence."""
import json
import shutil
import sqlite3

import pytest

from argos.console.archive import RecordingArchive
from argos.console.visual_recording import VisualArchive
from examples.verify_demo_flight import DEFAULT_DIRECTORY, main, verify_demo_flight


@pytest.fixture(scope="module")
def flight():
    manifest = json.loads((DEFAULT_DIRECTORY / "manifest.json").read_text())
    archive = VisualArchive(DEFAULT_DIRECTORY)
    binding = {"started_at": manifest["capture"]["started_at"],
               "run_id": manifest["capture"]["run_id"]}
    metadata = archive.metadata(manifest["id"], **binding)
    return manifest, archive, {**binding, "revision": metadata["revision"]}


def test_distributed_flight_and_every_jpeg_pass_real_archive_readers():
    report = verify_demo_flight()
    assert report["integrity"] == "verified"
    assert report["visual_frames"] > 100
    assert report["visual_dropped"] == 0
    assert report["chapters"] >= 7


def test_manifest_detects_an_asset_change_before_replay(tmp_path):
    manifest = json.loads((DEFAULT_DIRECTORY / "manifest.json").read_text())
    shutil.copy(DEFAULT_DIRECTORY / "manifest.json", tmp_path)
    source = DEFAULT_DIRECTORY / manifest["files"]["telemetry"]["path"]
    data = bytearray(source.read_bytes())
    data[-10] ^= 1  # Same size, different content.
    (tmp_path / source.name).write_bytes(data)
    with pytest.raises(ValueError, match="telemetry SHA-256"):
        verify_demo_flight(tmp_path)


def test_chapters_cannot_be_relabelled_as_different_recorded_events(tmp_path):
    shutil.copytree(DEFAULT_DIRECTORY, tmp_path, dirs_exist_ok=True)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["chapters"][0]["evidence"]["detail"] = "Land requested"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="recorded evidence"):
        verify_demo_flight(tmp_path)


def test_seek_backwards_never_exposes_future_frames_or_detections(flight):
    manifest, archive, access = flight
    identifier = manifest["id"]
    path = DEFAULT_DIRECTORY / manifest["files"]["visual"]["path"]
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as database:
        frames = database.execute(
            "SELECT idx,available,sequence,video_id,vision FROM frames ORDER BY idx").fetchall()
    first = archive.replay(identifier, 0., **access)
    assert first["state"] == "waiting" and first["frame"] is None
    start = access["started_at"]
    # Jump through the real availability boundaries in reverse, including the
    # instant before each image can be shown. Do not synthesize tracker output.
    for index, available, sequence, source, encoded_vision in reversed(frames):
        after = archive.replay(identifier, available - start + 1e-7, **access)
        frame = after["frame"]
        assert frame["index"] == index
        assert frame["available_at_s"] <= after["at_s"]
        assert frame["source_sequence"] == sequence and frame["video_id"] == source
        vision = json.loads(encoded_vision)
        assert frame["vision"] == vision
        if vision is not None:
            assert vision["frame_sequence"] == sequence and vision["video_id"] == source
            assert frame["detections"] == vision["detections"]
        before = archive.replay(identifier, max(0., available - start - 1e-7), **access)
        assert before["frame"] is None or before["frame"]["index"] < index


def test_chapters_include_requests_and_observed_state_without_hiding_status_text(flight):
    manifest, archive, access = flight
    chapters = {entry["id"]: entry for entry in manifest["chapters"]}
    assert chapters["land"]["evidence"]["status"] == "accepted"
    assert chapters["disarmed"]["evidence"]["status"] == "sampled"
    later = archive.replay(manifest["id"], chapters["disarmed"]["at_s"] + .001, **access)
    assert later["sample"]["control"]["vehicle"]["armed"] is False
    telemetry = RecordingArchive(DEFAULT_DIRECTORY)
    metadata = telemetry.metadata(manifest["id"])
    messages = telemetry.messages(manifest["id"], metadata["revision"], message_id=253)
    texts = {entry["fields"]["text"] for entry in messages["items"]}
    assert {"GCS Failsafe", "GCS Failsafe Cleared", "Arming motors", "Disarming motors"} <= texts


def test_verifier_cli_reports_a_missing_fixture_without_traceback(tmp_path, capsys):
    assert main(["--directory", str(tmp_path)]) == 1
    assert "Demo verification failed" in capsys.readouterr().err
