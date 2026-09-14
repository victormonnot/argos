"""Local visual export publishes only complete, source-bound offline reports."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from argos.perception.evaluation_report import summarize
from examples import export_vision_comparison as exporter


def jpeg():
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(stream, format="JPEG")
    return stream.getvalue()


def data_rows():
    detection = {"box": [.1, .2, .3, .4], "confidence": .9, "track_id": 1}
    return [{"index": index + 5, "at_s": index * .2 - .05, "available_at_s": index * .2,
             "segment": 0, "sequence": index + 1, "video_id": "c" * 32,
             "width": 32, "height": 24, "jpeg_sha256": hashlib.sha256(jpeg()).hexdigest(),
             "unexpected_private_field": "/private/do-not-export",
             "models": {variant: {"detections": [] if variant == "tiny" and index == 1 else [detection],
                         "inference_ms": 2., "processing_ms": 3.} for variant in ("tiny", "s")}}
            for index in range(2)]


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    comparison, recordings = tmp_path / "comparison", tmp_path / "recordings"
    comparison.mkdir()
    recordings.mkdir()
    (comparison / "original.txt").write_text("keep comparison")
    (recordings / "original.txt").write_text("keep recording")
    rows = data_rows()
    report = {"source": {"id": "a" * 32, "environment": "simulation", "duration_s": 1.,
              "selected_frames": 2, "archive_frame_rows": 8, "truncated": False, "skipped_counts": {}},
              "models": {variant: {"label": variant, "input_size": 416,
                        "path": "/private/model/path"} for variant in ("tiny", "s")},
              "summary": summarize(rows)}
    calls = []
    source = SimpleNamespace(report=report, rows=rows, comparison_directory=comparison,
        source_directory=recordings, journal_revision="1" * 64, visual_revision="2" * 64,
        input_sha256={"report.json": "3" * 64, "frames.jsonl": "4" * 64},
        read_frame=lambda row: jpeg(), verify_unchanged=lambda: calls.append("verified"))
    monkeypatch.setattr(exporter, "load_comparison", lambda *args: source)
    source.calls = calls
    return source


def test_export_copies_exact_jpegs_and_embeds_only_relevant_data(tmp_path, loaded):
    output = exporter.export_view(loaded.comparison_directory, tmp_path / "view")
    html = (output / "index.html").read_text()
    assert exporter.PLACEHOLDER not in html
    assert "/private/" not in html
    assert '"differences":[1]' in html
    assert '"index":5' in html and '"at_s":-0.05' in html
    assert loaded.calls == ["verified"]
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["state"] == "complete" and manifest["inference_performed"] is False
    assert manifest["frames"] == 2 and manifest["count_disagreements"] == 1
    assert manifest["html_sha256"] == hashlib.sha256(html.encode()).hexdigest()
    for image in manifest["images"]:
        copied = (output / image["path"]).read_bytes()
        assert copied == jpeg()
        assert hashlib.sha256(copied).hexdigest() == image["sha256"]
    assert (loaded.comparison_directory / "original.txt").read_text() == "keep comparison"
    assert (loaded.source_directory / "original.txt").read_text() == "keep recording"


def test_inline_json_cannot_close_script_or_insert_html():
    malicious = '</script><script>window.injected=true</script>&\u2028\u2029'
    html, _ = exporter.render_html({"label": malicious})
    assert malicious not in html
    assert "\\u003c/script\\u003e" in html
    assert "\\u0026" in html and "\\u2028" in html and "\\u2029" in html


@pytest.mark.parametrize("exception", [ValueError("image binding changed"), KeyboardInterrupt()])
def test_partial_image_export_never_publishes_an_openable_view(tmp_path, loaded, exception):
    def fail(row):
        if row["index"] == 6:
            raise exception
        return jpeg()
    loaded.read_frame = fail
    output = tmp_path / "view"
    with pytest.raises(type(exception)):
        exporter.export_view(loaded.comparison_directory, output)
    assert not (output / "index.html").exists()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["state"] == ("interrupted" if isinstance(exception, KeyboardInterrupt) else "failed")


def test_source_change_after_images_are_written_prevents_publication(tmp_path, loaded):
    def fail():
        raise ValueError("changed source")
    loaded.verify_unchanged = fail
    output = tmp_path / "view"
    with pytest.raises(ValueError, match="changed source"):
        exporter.export_view(loaded.comparison_directory, output)
    assert not (output / "index.html").exists()
    assert json.loads((output / "manifest.json").read_text())["state"] == "failed"


def test_last_publication_failure_cannot_leave_a_complete_manifest(tmp_path, loaded, monkeypatch):
    replace = Path.replace
    def fail(path, target):
        if path.name == "index.html.partial":
            raise OSError("publication failed")
        return replace(path, target)
    monkeypatch.setattr(Path, "replace", fail)
    output = tmp_path / "view"
    with pytest.raises(OSError, match="publication failed"):
        exporter.export_view(loaded.comparison_directory, output)
    assert json.loads((output / "manifest.json").read_text())["state"] == "failed"
    assert not (output / "index.html").exists()


def test_complete_manifest_requires_published_html_and_failure_demotes_view(tmp_path, loaded, monkeypatch):
    write_manifest = exporter.write_manifest
    def fail(directory, manifest):
        if manifest["state"] == "complete":
            assert (directory / "index.html").is_file()
            assert json.loads((directory / "manifest.json").read_text())["state"] == "building"
            raise OSError("completion marker failed")
        write_manifest(directory, manifest)
    monkeypatch.setattr(exporter, "write_manifest", fail)
    output = tmp_path / "view"
    with pytest.raises(OSError, match="completion marker failed"):
        exporter.export_view(loaded.comparison_directory, output)
    assert not (output / "index.html").exists()
    assert (output / "index.html.partial").is_file()
    assert json.loads((output / "manifest.json").read_text())["state"] == "failed"


@pytest.mark.parametrize("source", ["comparison_directory", "source_directory"])
def test_no_output_inside_either_input_directory(tmp_path, loaded, source):
    with pytest.raises(ValueError, match="outside"):
        exporter.export_view(loaded.comparison_directory, getattr(loaded, source) / "view")


def test_nonempty_destination_is_never_overwritten(tmp_path, loaded):
    output = tmp_path / "view"
    output.mkdir()
    (output / "index.html").write_text("existing view")
    with pytest.raises(ValueError, match="new or empty"):
        exporter.export_view(loaded.comparison_directory, output)
    assert (output / "index.html").read_text() == "existing view"


def test_temporary_root_cannot_bypass_input_directory_protection(tmp_path, loaded, monkeypatch):
    monkeypatch.setattr(exporter.tempfile, "gettempdir", lambda: str(loaded.source_directory))
    with pytest.raises(ValueError, match="outside"):
        exporter.export_view(loaded.comparison_directory)


def test_relocated_archive_override_is_forwarded_and_cli_failure_returns_nonzero(tmp_path, monkeypatch):
    calls = []
    def failed(comparison, output, *, recordings_dir):
        calls.append((comparison, output, recordings_dir))
        raise ValueError("revision mismatch")
    monkeypatch.setattr(exporter, "export_view", failed)
    assert exporter.main(["--comparison-dir", "comparison", "--recordings-dir", "moved",
                          "--output-dir", str(tmp_path / "view")]) == 1
    assert calls == [(Path("comparison"), tmp_path / "view", Path("moved"))]
