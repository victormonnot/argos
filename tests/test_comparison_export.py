"""Saved comparison export uses validated native pixels, never new inference."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from argos.console.archive import RecordingArchive
from argos.console.visual_recording import VisualArchive
from argos.perception import comparison_export as export
from argos.perception.comparison_export import ComparisonExportError, load_comparison
from test_compare_vision import native_capture


def write_inputs(f):
    (f.comparison / "report.json").write_text(json.dumps(f.report) + "\n")
    (f.comparison / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in f.rows))


@pytest.fixture
def comparison(tmp_path):
    source, comparison = tmp_path / "recordings", tmp_path / "comparison"
    identifier, frames = native_capture(source)
    journal = RecordingArchive(source).metadata(identifier)
    binding = {"started_at": journal["started_at"], "run_id": journal["context"]["run_id"]}
    visual = VisualArchive(source)
    media = visual.metadata(identifier, **binding)
    rows = []
    for index, frame in enumerate(frames):
        item = visual.frame_record(identifier, index, revision=media["revision"], **binding)
        row = {key: value for key, value in item.items() if key != "jpeg"}
        row.update(jpeg_sha256=hashlib.sha256(item["jpeg"]).hexdigest(), segment=0,
                   excluded_before=False, at_s=item["received_at"] - journal["started_at"],
                   available_at_s=item["available_at"] - journal["started_at"],
                   models={variant: {"detections": [{"box": [.2, .1, .2, .5],
                            "confidence": .4 if variant == "tiny" else .9,
                            "track_id": 11 + 8 * index if variant == "tiny" else 1}],
                            "inference_ms": 2. + index, "processing_ms": 3. + index}
                           for variant in ("tiny", "s")})
        rows.append(row)
    report = {"format": "argos.vision-comparison", "version": 1, "state": "complete",
        "source": {"id": identifier, "directory": str(source), "environment": "real",
                   "video_source": "device", "duration_s": journal["duration_s"],
                   "telemetry_revision": journal["revision"], "visual_revision": media["revision"],
                   "archive_frame_rows": len(rows), "examined_frame_rows": len(rows),
                   "selected_frames": len(rows), "skipped_counts": {}, "truncated": False,
                   "unevaluated_frame_rows": 0, "visual_recording_dropped": 0},
        "configuration": {"threads": 2, "max_frames": 1000, "warmup_frames": 2,
                          "timed_passes": 1, "inference_order": "serial alternate"},
        "models": {variant: {"label": f"Historic {variant}", "sha256": "a" * 64,
                             "input_size": 416, "path": "/not-present/model.onnx"}
                   for variant in ("tiny", "s")},
        "provenance": {"source_sha256": {"historical.py": "e" * 64}},
        "summary": {"frame_count": 999}, "skipped_rows": [], "limits": ["No truth labels"]}
    comparison.mkdir()
    fixture = SimpleNamespace(comparison=comparison, source=source, report=report,
                              rows=rows, frames=frames, identifier=identifier)
    write_inputs(fixture)
    return fixture


def test_camera_only_capture_binds_exact_pixels_and_recomputes_stale_aggregates(comparison):
    f = comparison
    originals = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                 for directory in (f.source, f.comparison) for path in directory.iterdir()}
    loaded = load_comparison(f.comparison)
    assert loaded.source_directory == f.source
    assert loaded.journal_revision == f.report["source"]["telemetry_revision"]
    assert loaded.visual_revision == f.report["source"]["visual_revision"]
    assert loaded.report["summary"]["frame_count"] == 2
    # Weak display IDs may legitimately change on every sample. Validation does
    # not impose deterministic IDs, cross-model equality, or person identity.
    assert loaded.report["summary"]["variants"]["tiny"]["adjacent_single_detection_id_changes"] == 1
    assert loaded.report["summary"]["variants"]["s"]["adjacent_single_detection_id_changes"] == 0
    assert loaded.rows[0]["at_s"] < 0
    for row, frame in zip(loaded.rows, f.frames):
        assert loaded.read_frame(row) == frame["jpeg"]
    loaded.verify_unchanged()
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals} == originals
    assert not any(path.name.endswith(("-journal", "-wal")) for path in f.source.iterdir())


def test_relocated_native_archive_is_allowed_only_with_original_revisions(comparison, tmp_path):
    f = comparison
    moved = tmp_path / "relocated"
    shutil.copytree(f.source, moved)
    shutil.rmtree(f.source)
    loaded = load_comparison(f.comparison, recordings_dir=moved)
    assert loaded.source_directory == moved
    assert loaded.read_frame(loaded.rows[0]) == f.frames[0]["jpeg"]
    loaded.verify_unchanged()


@pytest.mark.parametrize("field,value", [
    ("state", "failed"), ("state", "running"), ("state", "interrupted"),
    ("format", "different"), ("version", 2), ("version", True),
])
def test_only_complete_supported_reports_are_accepted(comparison, field, value):
    comparison.report[field] = value
    write_inputs(comparison)
    with pytest.raises(ComparisonExportError):
        load_comparison(comparison.comparison)


@pytest.mark.parametrize("field,value", [
    ("id", "d" * 32), ("environment", "simulation"), ("video_source", "gazebo"),
    ("duration_s", 99.), ("telemetry_revision", "f" * 64), ("visual_revision", "f" * 64),
    ("selected_frames", 1), ("archive_frame_rows", 3),
])
def test_spoofed_source_metadata_cannot_bind_to_original_recording(comparison, field, value):
    comparison.report["source"][field] = value
    write_inputs(comparison)
    with pytest.raises(ComparisonExportError):
        load_comparison(comparison.comparison)


@pytest.mark.parametrize("field,value", [
    ("jpeg_sha256", "d" * 64), ("sequence", 90), ("width", 40),
    ("video_id", "d" * 32), ("received_at", 19.91), ("available_at", 20.01),
    ("at_s", 0.), ("available_at_s", .02),
])
def test_frame_identity_clock_and_digest_must_match_exact_archived_row(comparison, field, value):
    comparison.rows[0][field] = value
    write_inputs(comparison)
    with pytest.raises(ComparisonExportError):
        load_comparison(comparison.comparison)


@pytest.mark.parametrize("mutation", ["same_index", "reverse", "bool_index", "model_missing",
    "model_extra", "box_bounds", "zero_box", "confidence", "duplicate_id", "bool_id",
    "negative_time", "too_many_detections", "segment", "excluded"])
def test_malformed_paired_rows_are_rejected(comparison, mutation):
    f = comparison
    model = f.rows[0]["models"]["tiny"]
    detection = model["detections"][0]
    if mutation == "same_index": f.rows[1]["index"] = 0
    elif mutation == "reverse": f.rows.reverse()
    elif mutation == "bool_index": f.rows[0]["index"] = False
    elif mutation == "model_missing": del f.rows[0]["models"]["s"]
    elif mutation == "model_extra": f.rows[0]["models"]["other"] = deepcopy(model)
    elif mutation == "box_bounds": detection["box"] = [.9, .1, .2, .5]
    elif mutation == "zero_box": detection["box"][2] = 0
    elif mutation == "confidence": detection["confidence"] = 1.01
    elif mutation == "duplicate_id": model["detections"].append(deepcopy(detection))
    elif mutation == "bool_id": detection["track_id"] = True
    elif mutation == "negative_time": model["processing_ms"] = -1
    elif mutation == "too_many_detections": model["detections"] *= 17
    elif mutation == "segment": f.rows[1]["segment"] = 1
    elif mutation == "excluded": f.rows[0]["excluded_before"] = True
    write_inputs(f)
    with pytest.raises(ComparisonExportError):
        load_comparison(f.comparison)


@pytest.mark.parametrize("filename,replace", [
    ("report.json", lambda data: data.replace('"version": 1', '"version": 1, "version": 1')),
    ("frames.jsonl", lambda data: data.replace('"index": 0', '"index": 0, "index": 0')),
    ("frames.jsonl", lambda data: data.replace('"confidence": 0.4', '"confidence": NaN')),
    ("frames.jsonl", lambda data: data.replace('"confidence": 0.4', '"confidence": 1e999')),
])
def test_json_duplicate_keys_and_nonfinite_values_are_never_silently_accepted(comparison, filename, replace):
    path = comparison.comparison / filename
    original = path.read_text()
    changed = replace(original)
    assert changed != original
    path.write_text(changed)
    with pytest.raises(ComparisonExportError):
        load_comparison(comparison.comparison)


@pytest.mark.parametrize("filename", ["report.json", "frames.jsonl"])
def test_symlink_comparison_inputs_are_refused(comparison, filename):
    path = comparison.comparison / filename
    original = path.with_name(filename + ".original")
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(ComparisonExportError, match="symlinks"):
        load_comparison(comparison.comparison)


@pytest.mark.parametrize("change", ["report", "rows", "archive"])
def test_final_verification_catches_inputs_changed_during_export(comparison, change):
    f = comparison
    loaded = load_comparison(f.comparison)
    if change == "archive":
        with sqlite3.connect(f.source / f"{f.identifier}.visual.sqlite3") as db:
            value = json.loads(db.execute("SELECT metadata FROM recording").fetchone()[0])
            value["detail"] = "changed"
            db.execute("UPDATE recording SET metadata=?", (json.dumps(value),))
    else:
        filename = "report.json" if change == "report" else "frames.jsonl"
        with (f.comparison / filename).open("a") as output:
            output.write(" ")
    with pytest.raises(ComparisonExportError, match="changed"):
        loaded.verify_unchanged()


def test_mutated_selected_row_cannot_request_unbound_pixels(comparison):
    loaded = load_comparison(comparison.comparison)
    row = dict(loaded.rows[0], jpeg_sha256="a" * 64)
    with pytest.raises(ComparisonExportError, match="not bound"):
        loaded.read_frame(row)


def test_empty_partial_and_oversized_frame_line_are_clear_errors(comparison):
    path = comparison.comparison / "frames.jsonl"
    path.rename(path.with_name("frames.jsonl.partial"))
    with pytest.raises(ComparisonExportError, match="completed"):
        load_comparison(comparison.comparison)
    path.write_text("")
    with pytest.raises(ComparisonExportError, match="nonempty"):
        load_comparison(comparison.comparison)
    path.write_text(" " * (export.MAX_FRAME_LINE_BYTES + 1) + "\n")
    with pytest.raises(ComparisonExportError, match="32 KiB"):
        load_comparison(comparison.comparison)


def test_complete_input_bounds_are_enforced_before_loading_json(comparison, monkeypatch):
    monkeypatch.setattr(export, "MAX_REPORT_BYTES", 8)
    with pytest.raises(ComparisonExportError, match="bounded"):
        load_comparison(comparison.comparison)
