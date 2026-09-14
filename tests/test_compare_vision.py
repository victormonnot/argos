"""Native-image comparison, isolated inference, chronology and output integrity."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from PIL import Image
import pytest

from argos.console.config import ConsoleConfig
from argos.console.context import capture_context
from argos.console.recording import ConsoleRecorder
from examples import compare_vision as compare


RUN, VIDEO = "b" * 32, "c" * 32
START = 20.


def jpeg(color="green", size=(32, 24)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, "JPEG")
    return stream.getvalue()


def record(index=0, *, received=START, available=None, sequence=None,
           video_id=VIDEO, size=(32, 24), data=None):
    return {"index": index, "received_at": received,
            "available_at": START + .2 * index if available is None else available,
            "sequence": index + 1 if sequence is None else sequence,
            "video_id": video_id, "width": size[0], "height": size[1],
            "jpeg": data if data is not None else jpeg(size=size)}


def fake_images(monkeypatch, tmp_path, records, *, limit=1000):
    telemetry = {"started_at": START, "ended_at": START + 10, "duration_s": 10.,
                 "revision": "telemetry-one", "context": {"run_id": RUN,
                 "configuration": {"environment": "real", "video_source": "device"}}}
    visual = {"state": "complete", "frames": len(records), "ended_at": START + 10,
              "revision": "visual-one", "dropped": 0}
    calls = []
    class Journal:
        def __init__(self, directory):
            pass

        def metadata(self, identifier):
            return deepcopy(telemetry)

    class Visual:
        def __init__(self, directory):
            pass

        def metadata(self, identifier, **kwargs):
            return deepcopy(visual)

        def frame_record(self, identifier, index, **kwargs):
            calls.append(index)
            return dict(records[index])

    monkeypatch.setattr(compare, "RecordingArchive", Journal)
    monkeypatch.setattr(compare, "VisualArchive", Visual)
    value = compare.ArchiveImages(tmp_path, "a" * 32, limit)
    return SimpleNamespace(images=value, telemetry=telemetry, visual=visual, calls=calls)


def native_capture(directory, *, count=2):
    """Real native files, including a pre-start image and no MAVLink events."""
    config = ConsoleConfig(video_source="device", video_endpoint="/dev/video99",
                           environment="real", recordings_dir=directory)
    context = capture_context(config, RUN, None)
    recorder = ConsoleRecorder(directory)
    identifier = recorder.start(START, context=context, include_visual=True)["id"]
    frames = []
    for index in range(count):
        frame = record(index, received=START - .05 + .2 * index,
                       data=jpeg("green" if index % 2 == 0 else "red"))
        assert recorder.visual.append(frame["available_at"], frame=frame, vision=None, control=None)
        frames.append(frame)
    recorder.stop(START + max(1., count * .2))
    assert recorder.visual.wait(3.)["state"] == "complete"
    return identifier, frames


def tracked_result(identity=1):
    return {"detections": [{"track_id": identity, "confidence": .8, "box": [.3, .2, .2, .5]}],
            "inference_ms": 2., "processing_ms": 3.}


def fake_pipeline_class(calls, *, error=None):
    class FakePipeline:
        def __init__(self, path, variant, threads):
            self.variant = variant
            calls.append(("initialize", variant, threads))

        def warmup(self, data):
            calls.append(("warmup", self.variant, data))

        def process(self, frame):
            calls.append(("process", self.variant, frame["jpeg"]))
            if error is not None and self.variant == "s" and frame["index"] >= 1:
                raise error
            return tracked_result()
    return FakePipeline


def cli_args(directory, identifier, output, *extra):
    return ["--recordings-dir", str(directory), "--recording", identifier,
            "--output-dir", str(output), *extra]


def test_duplicate_analyzed_image_does_not_create_a_second_evaluation(monkeypatch, tmp_path):
    first = record()
    duplicate = {**first, "index": 1, "available_at": START + .2}
    last = record(2, received=START + .4)
    f = fake_images(monkeypatch, tmp_path, [first, duplicate, last])
    rows = list(f.images)
    assert [row["index"] for row in rows] == [0, 2]
    assert f.images.skipped == [{"index": 1, "reason": "duplicate_image"}]
    assert rows[0]["jpeg_sha256"] == hashlib.sha256(first["jpeg"]).hexdigest()
    assert f.images.description()["selected_frames"] == 2
    assert f.images.description()["examined_frame_rows"] == 3


def test_same_image_identity_with_conflicting_jpeg_is_an_error(monkeypatch, tmp_path):
    first = record()
    conflict = {**first, "index": 1, "available_at": START + .2, "jpeg": jpeg("red")}
    f = fake_images(monkeypatch, tmp_path, [first, conflict])
    with pytest.raises(ValueError, match="conflicting JPEG"):
        list(f.images)


def test_regressing_and_equal_receipts_are_excluded_without_retiming_or_reordering(monkeypatch, tmp_path):
    rows = [record(0, received=20.1, available=20.2), record(1, received=20.0, available=20.4),
            record(2, received=20.1, available=20.6), record(3, received=20.6, available=20.8)]
    f = fake_images(monkeypatch, tmp_path, rows)
    chosen = list(f.images)
    assert [row["index"] for row in chosen] == [0, 3]
    assert [row["received_at"] for row in chosen] == [20.1, 20.6]
    assert [row["available_at"] for row in chosen] == [20.2, 20.8]
    assert f.images.description()["skipped_counts"] == {"non_increasing_receipt": 2}
    assert [row["reset_tracker"] for row in chosen] == [True, False]


def test_negative_start_offset_preserves_absolute_tracker_clock(monkeypatch, tmp_path):
    f = fake_images(monkeypatch, tmp_path, [record(received=19.95)])
    frame = next(iter(f.images))
    assert frame["at_s"] == pytest.approx(-.05)
    assert frame["received_at"] == 19.95
    assert frame["available_at_s"] == 0.


def test_dimension_and_source_changes_reset_only_accepted_context(monkeypatch, tmp_path):
    rows = [record(0, received=20.1, available=20.1), record(1, received=20.2, size=(48, 32)),
            record(2, received=20.0, size=(48, 32)),
            record(3, received=20.3, size=(48, 32), video_id="d" * 32),
            record(4, received=20.4, size=(48, 32), video_id="d" * 32)]
    f = fake_images(monkeypatch, tmp_path, rows)
    chosen = list(f.images)
    assert [row["index"] for row in chosen] == [0, 1, 3, 4]
    assert [row["segment"] for row in chosen] == [0, 1, 2, 2]
    assert [row["reset_tracker"] for row in chosen] == [True, True, True, False]


def test_limit_counts_unique_chronological_images_and_reports_unread_tail(monkeypatch, tmp_path):
    first = record()
    rows = [first, {**first, "index": 1, "available_at": 20.2},
            record(2, received=20.4), record(3, received=20.6)]
    f = fake_images(monkeypatch, tmp_path, rows, limit=2)
    assert [row["index"] for row in f.images] == [0, 2]
    description = f.images.description()
    assert f.calls == [0, 1, 2]
    assert description["truncated"]
    assert description["unevaluated_frame_rows"] == 1
    assert description["selected_frames"] == 2


@pytest.mark.parametrize("limit", [0, -1, 5001, True, 1.5])
def test_invalid_work_limits_are_rejected_before_archive_io(tmp_path, limit):
    with pytest.raises(ValueError, match="max_frames"):
        compare.ArchiveImages(tmp_path, "a" * 32, limit)


@pytest.mark.parametrize("changed", ["telemetry", "visual"])
def test_final_check_rejects_changed_source_revision(monkeypatch, tmp_path, changed):
    f = fake_images(monkeypatch, tmp_path, [record()])
    list(f.images)
    getattr(f, changed)["revision"] = "changed"
    with pytest.raises(ValueError, match="changed during evaluation"):
        f.images.verify_unchanged()


def test_pipeline_uses_production_tracker_with_all_detections_and_aligned_appearance(monkeypatch):
    calls = []
    detections = [{"confidence": .9, "box": [.2, .2, .15, .4]},
                  {"confidence": .4, "box": [.7, .2, .15, .4]}]
    class Detector:
        def __init__(self, path, *, variant, threads):
            calls.append(("detector", path, variant, threads))

        def detect(self, data):
            calls.append(("detect", data))
            return {"width": 32, "height": 24, "detections": deepcopy(detections), "inference_ms": 1.}

    class Encoder:
        def encode(self, data, boxes, *, width, height):
            calls.append(("appearance", data, deepcopy(boxes), width, height))
            return [None] * len(boxes)

    monkeypatch.setattr(compare, "YoloXPersonDetector", Detector)
    monkeypatch.setattr(compare, "AppearanceEncoder", Encoder)
    tiny = compare.Pipeline(Path("tiny.onnx"), "tiny", 4)
    small = compare.Pipeline(Path("s.onnx"), "s", 4)
    frame = {**record(received=19.95), "reset_tracker": True}
    tiny.warmup(frame["jpeg"])
    assert tiny.tracker._last_at is None and tiny.tracker._tracks == {}
    assert tiny.tracker._next_id == 1
    first = tiny.process(frame)
    other = small.process(frame)
    assert tiny.tracker is not small.tracker
    assert tiny.tracker._last_at == small.tracker._last_at == 19.95
    assert [d["confidence"] for d in first["detections"]] == [.9, .4]
    assert [d["track_id"] for d in first["detections"]] == [1, 2]
    assert [d["track_id"] for d in other["detections"]] == [1, 2]
    # Supplying even [None, None] enters production appearance validation mode.
    assert tiny.tracker._appearance_mode and small.tracker._appearance_mode
    assert all(call[2] == detections for call in calls if call[0] == "appearance")
    assert first["processing_ms"] >= 0 and first["inference_ms"] == 1.
    reset_frame = {**record(1, received=19.9), "reset_tracker": True}
    tiny.process(reset_frame)
    assert tiny.tracker._last_at == 19.9  # reset allows a new independent source clock
    assert small.tracker._last_at == 19.95


def test_evaluate_uses_same_exact_jpeg_pairs_and_alternates_execution_order(monkeypatch, tmp_path):
    f = fake_images(monkeypatch, tmp_path, [record(0), record(1, received=20.2, data=jpeg("red")),
                                          record(2, received=20.4, data=jpeg("blue"))])
    calls = []
    Pipeline = fake_pipeline_class(calls)
    pipelines = {variant: Pipeline(None, variant, 2) for variant in compare.VARIANTS}
    output = io.StringIO()
    rows = compare.evaluate(f.images, pipelines, output)
    work = [call for call in calls if call[0] == "process"]
    assert [call[1] for call in work] == ["tiny", "s", "s", "tiny", "tiny", "s"]
    assert all(work[index][2] is work[index + 1][2] for index in range(0, 6, 2))
    assert len([call for call in calls if call[0] == "warmup"]) == 2
    assert len(rows) == 3 and len(output.getvalue().splitlines()) == 3
    assert all("jpeg" not in row and "reset_tracker" not in row for row in rows)
    assert all(list(row["models"]) == ["tiny", "s"] for row in rows)


def test_camera_only_native_capture_succeeds_through_mocked_inference_cli(tmp_path, monkeypatch):
    source, output = tmp_path / "recordings", tmp_path / "comparison"
    identifier, frames = native_capture(source)
    original = {path.name: path.read_bytes() for path in source.iterdir()}
    images = compare.ArchiveImages(source, identifier, 1000)
    assert images.telemetry["events"] == 0 and images.telemetry["sources"] == []
    calls = []
    monkeypatch.setattr(compare, "Pipeline", fake_pipeline_class(calls))
    monkeypatch.setattr(compare, "provenance", lambda: {"inference": "mocked test"})
    assert compare.main(cli_args(source, identifier, output, "--threads", "4")) == 0
    report = json.loads((output / "report.json").read_text())
    rows = [json.loads(line) for line in (output / "frames.jsonl").read_text().splitlines()]
    assert report["state"] == "complete" and report["source"]["environment"] == "real"
    assert report["source"]["selected_frames"] == len(frames)
    assert rows[0]["at_s"] == pytest.approx(-.05)
    assert rows[0]["received_at"] == 19.95
    assert (output / "report.md").is_file() and not (output / "frames.jsonl.partial").exists()
    assert {path.name: path.read_bytes() for path in source.iterdir()} == original


def test_native_availability_regression_is_refused_by_archive_validation(tmp_path):
    source = tmp_path / "recordings"
    identifier, _ = native_capture(source)
    with sqlite3.connect(source / f"{identifier}.visual.sqlite3") as db:
        db.execute("UPDATE frames SET available=? WHERE idx=1", (START - .01,))
    with pytest.raises(Exception, match="[Ii]nvalid|[Vv]isual|finite|between"):
        compare.ArchiveImages(source, identifier, 1000)


def test_actual_native_sidecar_mutation_before_final_check_prevents_success(tmp_path):
    source = tmp_path / "recordings"
    identifier, _ = native_capture(source, count=1)
    images = compare.ArchiveImages(source, identifier, 1000)
    class Mutation:
        def warmup(self, data):
            pass

        def process(self, frame):
            with sqlite3.connect(source / f"{identifier}.visual.sqlite3") as db:
                meta = json.loads(db.execute("SELECT metadata FROM recording").fetchone()[0])
                meta["detail"] = "changed during evaluation"
                db.execute("UPDATE recording SET metadata=?", (json.dumps(meta),))
            return tracked_result()
    with pytest.raises(ValueError, match="changed during evaluation"):
        compare.evaluate(images, {variant: Mutation() for variant in compare.VARIANTS}, io.StringIO())


def test_new_and_existing_empty_output_directories_are_allowed(tmp_path):
    source = tmp_path / "source"
    fresh = tmp_path / "fresh"
    assert compare.create_output(fresh, source) == fresh
    assert fresh.is_dir()
    assert compare.create_output(fresh, source) == fresh


@pytest.mark.parametrize("relative", ["", "nested"])
def test_output_cannot_be_inside_source_recordings(tmp_path, relative):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="outside"):
        compare.create_output(source / relative, source)


def test_output_symlink_into_source_is_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        compare.create_output(alias, source)


def test_implicit_temporary_output_cannot_land_inside_source(tmp_path, monkeypatch):
    source = tmp_path / "recordings"
    source.mkdir()
    monkeypatch.setattr(compare.tempfile, "gettempdir", lambda: str(source))
    with pytest.raises(ValueError, match="outside"):
        compare.create_output(None, source)
    assert list(source.iterdir()) == []


def test_existing_artifacts_are_never_overwritten_even_on_cli_failure(tmp_path, monkeypatch):
    source, output = tmp_path / "recordings", tmp_path / "comparison"
    identifier, _ = native_capture(source)
    output.mkdir()
    artifacts = {"report.json": b"keep this report", "frames.jsonl": b"keep these frames", "report.md": b"keep this text"}
    for name, data in artifacts.items():
        (output / name).write_bytes(data)
    monkeypatch.setattr(compare, "Pipeline", lambda *a, **kw: pytest.fail("Existing output must reject before inference"))
    assert compare.main(cli_args(source, identifier, output)) == 1
    assert {path.name: path.read_bytes() for path in output.iterdir()} == artifacts


@pytest.mark.parametrize("error,state,code", [(RuntimeError("inference failed"), "failed", 1),
                                            (KeyboardInterrupt(), "interrupted", 130)])
def test_processing_failure_or_interruption_never_advertises_complete(tmp_path, monkeypatch, error, state, code):
    source, output = tmp_path / "recordings", tmp_path / "comparison"
    identifier, _ = native_capture(source)
    original = {path.name: path.read_bytes() for path in source.iterdir()}
    monkeypatch.setattr(compare, "Pipeline", fake_pipeline_class([], error=error))
    monkeypatch.setattr(compare, "provenance", lambda: {"inference": "mocked test"})
    assert compare.main(cli_args(source, identifier, output)) == code
    report = json.loads((output / "report.json").read_text())
    assert report["state"] == state
    assert not (output / "frames.jsonl").exists() and not (output / "report.md").exists()
    assert (output / "frames.jsonl.partial").is_file()
    assert len((output / "frames.jsonl.partial").read_text().splitlines()) == 1
    assert {path.name: path.read_bytes() for path in source.iterdir()} == original


def test_model_initialization_failure_leaves_failed_report_without_measurements(tmp_path, monkeypatch):
    source, output = tmp_path / "recordings", tmp_path / "comparison"
    identifier, _ = native_capture(source)
    def fail(*args, **kwargs):
        raise ValueError("verified model is missing")
    monkeypatch.setattr(compare, "Pipeline", fail)
    monkeypatch.setattr(compare, "provenance", lambda: {"inference": "mocked test"})
    assert compare.main(cli_args(source, identifier, output)) == 1
    assert json.loads((output / "report.json").read_text())["state"] == "failed"
    assert not (output / "frames.jsonl").exists()


@pytest.mark.parametrize("error,state,code", [(OSError("final publication failed"), "failed", 1),
                                            (KeyboardInterrupt(), "interrupted", 130)])
def test_final_report_commit_failure_demotes_all_completed_artifacts(tmp_path, monkeypatch, error, state, code):
    source, output = tmp_path / "recordings", tmp_path / "comparison"
    identifier, _ = native_capture(source, count=1)
    monkeypatch.setattr(compare, "Pipeline", fake_pipeline_class([]))
    monkeypatch.setattr(compare, "provenance", lambda: {"inference": "mocked test"})
    write_json = compare.write_json
    def fail_complete(path, report):
        if report["state"] == "complete":
            raise error
        write_json(path, report)
    monkeypatch.setattr(compare, "write_json", fail_complete)
    assert compare.main(cli_args(source, identifier, output)) == code
    assert json.loads((output / "report.json").read_text())["state"] == state
    assert not (output / "report.md").exists() and not (output / "frames.jsonl").exists()
    assert (output / "report.md.partial").is_file() and (output / "frames.jsonl.partial").is_file()


def test_rendering_failure_retains_partial_measurements_without_completed_report(tmp_path, monkeypatch):
    from argos.perception import evaluation_report
    source, output = tmp_path / "recordings", tmp_path / "comparison"
    identifier, _ = native_capture(source, count=1)
    monkeypatch.setattr(compare, "Pipeline", fake_pipeline_class([]))
    monkeypatch.setattr(compare, "provenance", lambda: {"inference": "mocked test"})
    def fail(report):
        raise RuntimeError("rendering failed")
    monkeypatch.setattr(evaluation_report, "render_markdown", fail)
    assert compare.main(cli_args(source, identifier, output)) == 1
    assert json.loads((output / "report.json").read_text())["state"] == "failed"
    assert not (output / "report.md").exists() and not (output / "frames.jsonl").exists()
    assert (output / "frames.jsonl.partial").is_file()


def test_zero_frame_native_capture_is_not_a_completed_comparison(tmp_path):
    source, output = tmp_path / "recordings", tmp_path / "comparison"
    identifier, _ = native_capture(source, count=0)
    assert compare.main(cli_args(source, identifier, output)) == 1
    assert not output.exists()
