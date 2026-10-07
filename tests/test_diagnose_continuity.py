"""Native provenance checks and the offline CLI's measured-input handoff."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from examples import diagnose_continuity as diagnostic


def write_json(path, value):
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def write_lines(path, values):
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def update_index(directory, rows):
    write_lines(directory / "camera.frames.jsonl", rows)
    camera = json.loads((directory / "camera.json").read_text())
    camera["index_bytes"] = (directory / "camera.frames.jsonl").stat().st_size
    write_json(directory / "camera.json", camera)


def update_events(directory, events):
    write_lines(directory / "events.jsonl", events)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest.update(events=len(events), bytes=(directory / "events.jsonl").stat().st_size)
    write_json(directory / "manifest.json", manifest)


@pytest.fixture
def native(tmp_path):
    directory = tmp_path / "sample.flight"
    directory.mkdir()
    # JPEG envelopes suffice for reader tests; inference uses the detector stub
    # below. These are never presented as decoded image fixtures.
    images = [b"\xff\xd8" + bytes([index]) * 8 + b"\xff\xd9" for index in range(3)]
    rows, events = [], []
    offset = 0
    for index, image in enumerate(images):
        at = 100. + (index + 1) / 8
        rows.append(dict(schema=1, session_id="capture", frame=index, sequence=10 + 2 * index,
            received_at=at, elapsed_s=at - 100., offset=offset, size_bytes=len(image),
            source="device", source_id="camera", width=64, height=48))
        offset += len(image)
        events.append(dict(schema_version=1, kind="vision", run_id="run", recording_id="capture",
            at=at + 1 / 32, frame_sequence=10 + 2 * index, image_received_at=at, video_id="camera",
            result=dict(width=64, height=48, inference_ms=2.,
                detections=[dict(box=[.1, .2, .2, .5], confidence=.9, track_id=7)])))
    (directory / "camera.mjpeg").write_bytes(b"".join(images))
    write_lines(directory / "camera.frames.jsonl", rows)
    write_lines(directory / "events.jsonl", events)
    write_json(directory / "camera.json", dict(schema=1, state="complete", session_id="capture",
        writer_stopped=True, error="", discarded_error=0, started_at=100., ended_at=101.,
        media="camera.mjpeg", index="camera.frames.jsonl", format="mjpeg_with_receive_timestamp_index",
        written_frames=3, accepted_frames=3, buffered_frames=0, buffered_bytes=0,
        size_bytes=offset, index_bytes=(directory / "camera.frames.jsonl").stat().st_size,
        dropped_frames=2, dropped_queue=0, dropped_contention=2, dropped_invalid=0, dropped_limit=0,
        complete=False, first_received_at=rows[0]["received_at"], last_received_at=rows[-1]["received_at"]))
    write_json(directory / "manifest.json", dict(format="argos.filming", schema_version=1,
        state="complete", writer_stopped=True, error="", failed_events=0, pending_events=0,
        id="capture", run_id="run", started_at=100., ended_at=101., complete=False,
        events=len(events), bytes=(directory / "events.jsonl").stat().st_size, dropped_events=1))
    return directory


def test_native_reader_preserves_exact_slices_times_and_declared_drops(native):
    archive = diagnostic.NativeFlight(native)
    frames = list(archive.read_frames([dict(start=.125, end=.5)], 3))
    assert [frame["sequence"] for frame in frames] == [10, 12, 14]
    assert [frame["received_at"] for frame in frames] == [.125, .25, .375]
    assert frames[0]["available_at"] == .15625
    assert frames[0]["sha256"] == hashlib.sha256(frames[0]["jpeg"]).hexdigest()
    assert b"".join(frame["jpeg"] for frame in frames) == (native / "camera.mjpeg").read_bytes()
    assert archive.drop_counts == dict(camera_frames=2, events=1)
    archive.verify()
    (native / "camera.mjpeg").write_bytes(b"changed")
    with pytest.raises(ValueError, match="source changed"):
        archive.verify()


@pytest.mark.parametrize("change", ["middle", "tail", "offset", "ordinal", "media_tail"])
def test_missing_or_misindexed_originals_cannot_become_fake_temporal_gaps(native, change):
    rows = read_lines(native / "camera.frames.jsonl")
    if change == "middle":
        del rows[1]
    elif change == "tail":
        rows.pop()
    elif change == "offset":
        rows[1]["offset"] += 1
    elif change == "ordinal":
        rows[1]["frame"] += 1
    else:
        media = native / "camera.mjpeg"
        media.write_bytes(media.read_bytes() + b"unindexed")
        camera = json.loads((native / "camera.json").read_text())
        camera["size_bytes"] = media.stat().st_size
        write_json(native / "camera.json", camera)
    # Reconcile the superficial file size to exercise the structural checks.
    update_index(native, rows)
    with pytest.raises(ValueError, match="camera index"):
        diagnostic.NativeFlight(native)


@pytest.mark.parametrize("field,value", [("session_id", "other"), ("writer_stopped", False),
    ("error", "writer failed"), ("started_at", 99.), ("written_frames", 4), ("dropped_frames", 0)])
def test_native_camera_binding_finalization_and_counts_are_checked(native, field, value):
    path = native / "camera.json"
    camera = json.loads(path.read_text())
    camera[field] = value
    write_json(path, camera)
    with pytest.raises(ValueError):
        diagnostic.NativeFlight(native)


@pytest.mark.parametrize("field,value", [("at", 102.), ("at", 99.), ("run_id", "other"),
    ("recording_id", "other"), ("image_received_at", 100.01), ("video_id", "other")])
def test_historical_events_cannot_invent_availability_or_cross_sources(native, field, value):
    events = read_lines(native / "events.jsonl")
    events[0][field] = value
    update_events(native, events)
    with pytest.raises(ValueError):
        diagnostic.NativeFlight(native)


def test_regressing_vision_sequence_is_rejected_even_when_log_times_increase(native):
    events = read_lines(native / "events.jsonl")
    events[0]["frame_sequence"], events[1]["frame_sequence"] = events[1]["frame_sequence"], events[0]["frame_sequence"]
    events[0]["image_received_at"], events[1]["image_received_at"] = events[1]["image_received_at"], events[0]["image_received_at"]
    events[0]["at"], events[1]["at"] = 100.4, 100.5
    update_events(native, events)
    with pytest.raises(ValueError, match="timeline/binding"):
        diagnostic.NativeFlight(native)


@pytest.mark.parametrize("change", ["short_box", "negative_box", "confidence", "nan", "dimensions"])
def test_invalid_historical_measurements_are_rejected_before_parity(native, change):
    events = read_lines(native / "events.jsonl")
    result = events[0]["result"]
    box = result["detections"][0]
    if change == "short_box":
        box["box"].pop()
    elif change == "negative_box":
        box["box"][0] = -.1
    elif change == "confidence":
        box["confidence"] = 1.1
    elif change == "nan":
        box["confidence"] = float("nan")
    else:
        result["width"] = 32
    update_events(native, events)
    with pytest.raises(ValueError):
        diagnostic.NativeFlight(native)


def test_native_event_truncation_is_rejected(native):
    events = read_lines(native / "events.jsonl")[:-1]
    write_lines(native / "events.jsonl", events)
    manifest = json.loads((native / "manifest.json").read_text())
    manifest["bytes"] = (native / "events.jsonl").stat().st_size
    write_json(native / "manifest.json", manifest)
    with pytest.raises(ValueError, match="event count"):
        diagnostic.NativeFlight(native)


@pytest.mark.parametrize("flag,value", [("--max-hz", "10.0"), ("--max-hz", "0"),
    ("--threads", "0"), ("--extra-worker-ms", "nan"), ("--extra-worker-ms", "-1")])
def test_cli_invalid_configuration_fails_before_reading_or_inferring(monkeypatch, tmp_path, flag, value):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid configuration reached source loading/inference")
    monkeypatch.setattr(diagnostic, "read_windows", forbidden)
    monkeypatch.setattr(diagnostic, "NativeFlight", forbidden)
    monkeypatch.setattr(diagnostic, "infer", forbidden)
    with pytest.raises(SystemExit) as failure:
        diagnostic.main(["--flight-dir", str(tmp_path), "--windows", str(tmp_path / "missing.json"),
                         "--output-dir", str(tmp_path / "output"), flag, value])
    assert failure.value.code != 0


@pytest.mark.parametrize("replace_model", [False, True])
def test_cli_uses_loaded_model_hash_and_stresses_only_simulated_mode(native, tmp_path, monkeypatch, replace_model):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"verified model bytes")
    loaded_hash = diagnostic.digest(model)
    windows = tmp_path / "windows.json"
    write_json(windows, dict(windows=[dict(name="loss", start=.125, end=.5,
        selection=dict(at=.2, box=[.1, .2, .2, .5]))]))
    output = tmp_path / "output"

    class Detector:
        def __init__(self, *args, **kwargs):
            self.model = SimpleNamespace(sha256=loaded_hash)
        def decode_jpeg(self, data):
            if replace_model:
                model.write_bytes(b"replaced after loading")
            return data
        def detect_bgr(self, image):
            return dict(width=64, height=48, inference_ms=1.,
                        detections=[dict(box=[.1, .2, .2, .5], confidence=.9)])

    modes = []
    def replay(frames, **kwargs):
        modes.append((kwargs["mode"], kwargs["extra_worker_ms"], kwargs["max_hz"]))
        return {"mode": kwargs["mode"]}

    monkeypatch.setattr(diagnostic, "YoloXPersonDetector", Detector)
    monkeypatch.setattr(diagnostic, "AppearanceEncoder", lambda: SimpleNamespace(encode_bgr=lambda *args, **kwargs: [None]))
    monkeypatch.setattr(diagnostic, "replay", replay)
    monkeypatch.setattr(diagnostic, "save_viewer", lambda *args: None)
    args = ["--flight-dir", str(native), "--windows", str(windows), "--output-dir", str(output),
            "--model-path", str(model), "--extra-worker-ms", "600"]
    if replace_model:
        with pytest.raises(SystemExit):
            diagnostic.main(args)
    else:
        assert diagnostic.main(args) == 0
    report = json.loads((output / "report.json").read_text())
    assert report["model"]["sha256"] == loaded_hash
    assert modes == [("recorded", 0., 10), ("simulated", 600., 10)]
    assert report["source"]["declared_drops"] == dict(camera_frames=2, events=1)
    assert report["state"] == ("failed" if replace_model else "complete")
    if replace_model:
        assert "model file changed" in report["error"]
        assert not (output / "inference.jsonl").exists()
    else:
        assert report["detection_parity"]["different_frames_at_1e_5"] == 0
        assert (output / "inference.jsonl").exists()
