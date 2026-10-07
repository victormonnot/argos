"""Offline viewer binds saved outputs to originals without running inference."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import pytest

from argos.perception import custom_comparison_export as export
from examples import compare_custom_vision as compare
from test_compare_custom_vision import frames_manifest
from test_compare_vision import tracked_result


@pytest.fixture
def comparison(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    manifest, originals = frames_manifest(source)
    images = compare.ManifestImages(manifest)
    rows = []
    for frame in images:
        row = {k: v for k, v in frame.items() if k not in {"jpeg", "reset_tracker"}}
        row["models"] = {name: tracked_result() for name in compare.MODELS}
        rows.append(row)
    directory = tmp_path / "comparison"
    directory.mkdir()
    report = {"format": "argos.custom-vision-comparison", "version": 1, "state": "complete",
              "source": images.description(), "models": {
                  "baseline": {"variant": "nano", "sha256": "a" * 64},
                  "candidate": {"variant": "custom", "sha256": "b" * 64}},
              "summary_by_split": compare.summarize(rows), "limits": ["No identity reference"]}
    (directory / "report.json").write_text(json.dumps(report))
    (directory / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return directory, report, rows, manifest


def write_rows(directory, rows):
    (directory / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_export_keeps_original_bytes_and_all_frames_without_inference(comparison, tmp_path, monkeypatch):
    directory, report, rows, manifest = comparison
    monkeypatch.setattr(compare, "CustomPipeline", lambda *a, **kw: pytest.fail("inference started"))
    monkeypatch.setattr(compare, "ComparisonPipeline", lambda *a, **kw: pytest.fail("inference started"))
    before = {path: path.read_bytes() for path in directory.iterdir()}
    output = tmp_path / "viewer"
    receipt = export.export_comparison(directory, output)
    html = (output / "index.html").read_text()
    assert receipt["frames"] == 3 and receipt["all_comparison_frames_included"]
    assert receipt["inference_performed"] is False and receipt["html_bytes"] < export.MAX_HTML_BYTES
    assert hashlib.sha256((output / "index.html").read_bytes()).hexdigest() == receipt["html_sha256"]
    data = json.loads(re.search(r'<script type="application/json" id="comparison-data">(.*?)</script>', html, re.S)[1])
    for row in rows:
        original = Path(row["path"])
        if not original.is_absolute():
            original = manifest.parent / original
        encoded = data["images"][row["sha256"]].split(",", 1)[1]
        assert base64.b64decode(encoded) == original.read_bytes()
    assert len(data["frames"]) == 3
    assert all(path.read_bytes() == content for path, content in before.items())
    assert "https://" not in html and "fetch(" not in html


def test_checksum_change_rejected_before_output_creation(comparison, tmp_path):
    directory, report, rows, manifest = comparison
    (manifest.parent / "frame1.jpg").write_bytes(b"changed")
    output = tmp_path / "viewer"
    with pytest.raises(ValueError, match="SHA-256"):
        export.export_comparison(directory, output)
    assert not output.exists()


def test_manifest_change_rejected(comparison):
    directory, report, rows, manifest = comparison
    manifest.write_text(manifest.read_text() + "\n")
    with pytest.raises(ValueError, match="manifest changed"):
        export.load_comparison(directory)


@pytest.mark.parametrize("change,match", [
    (lambda rows: rows[0].update(jpeg_sha256="f" * 64), "hash differs"),
    (lambda rows: rows[0].update(boxes=[]), "differs from source"),
    (lambda rows: rows[0].update(received_at=100.), "timestamp"),
    (lambda rows: rows[0]["models"]["candidate"]["detections"][0].update(confidence=float("nan")), "Non-finite"),
    (lambda rows: rows[0]["models"]["candidate"]["detections"][0].update(box=[0, 0, 2, 2]), "coordinate"),
    (lambda rows: rows.pop(), "count"),
])
def test_unbound_or_invalid_saved_results_rejected(comparison, change, match):
    directory, report, rows, manifest = comparison
    change(rows)
    write_rows(directory, rows)
    with pytest.raises(ValueError, match=match):
        export.load_comparison(directory)


def test_invented_unannotated_metrics_rejected(comparison):
    directory, report, rows, manifest = comparison
    report["summary_by_split"]["val"]["candidate"]["reference"]["fp"] = 1
    (directory / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Unannotated"):
        export.load_comparison(directory)


def test_failed_comparison_and_existing_output_are_never_published(comparison, tmp_path):
    directory, report, rows, manifest = comparison
    existing = tmp_path / "viewer"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="never overwritten"):
        export.export_comparison(directory, existing)
    assert (existing / "keep.txt").read_text() == "keep"
    report["state"] = "failed"
    (directory / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="completed"):
        export.export_comparison(directory, tmp_path / "new")


def test_hostile_saved_labels_are_json_escaped_and_script_csp_hash_matches(comparison):
    directory, report, rows, manifest = comparison
    value = export.load_comparison(directory)
    hostile = '</script><script>window.unwanted=1</script><img src="https://example.invalid/">'
    value["rows"][0]["id"] = hostile
    value["report"]["limits"] = [hostile]
    html = export.render_comparison(value).decode()
    assert hostile not in html and "\\u003c/script\\u003e" in html
    assert "innerHTML" not in html
    script = html.split('<script id="viewer-script">', 1)[1].split("</script>", 1)[0]
    expected = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{expected}'" in html


def test_budget_fails_instead_of_subsampling_images(comparison, tmp_path, monkeypatch):
    directory, report, rows, manifest = comparison
    monkeypatch.setattr(export, "MAX_HTML_BYTES", 1024)
    with pytest.raises(ValueError, match="budget"):
        export.export_comparison(directory, tmp_path / "viewer")


def test_source_boundaries_and_frame_order_are_preserved_in_viewer(comparison):
    directory, report, rows, manifest = comparison
    value = export.load_comparison(directory)
    assert [r["clip_id"] for r in value["rows"]] == ["one", "one", "two"]
    html = export.render_comparison(value).decode()
    assert "frame.captured_at" in html and "frame.clip_id" in html
    assert "data.frames" in html
