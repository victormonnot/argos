"""Offline HTML from saved custom-comparison results, with original image bytes.

No detector, tracker, service or inference runtime is imported or started here.
"""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path

from .comparison_export import (
    ComparisonExportError, _fail, _hex, _integer, _json, _number, _object, _read_input, _text,
)

MAX_HTML_BYTES = 64 * 1024 * 1024
MAX_ROWS = 5000
MODELS = ("baseline", "candidate")
TEMPLATE = Path(__file__).with_name("static") / "custom_vision_comparison.html"


def _detections(value):
    if not isinstance(value, list) or len(value) > 16:
        _fail("Each model needs at most 16 saved detections")
    for item in value:
        _object(item, "saved detection")
        box = item.get("box")
        if not isinstance(box, list) or len(box) != 4:
            _fail("Saved detection needs a normalized xywh box")
        for number in box:
            _number(number, "box coordinate", minimum=0, maximum=1)
        if box[2] <= 0 or box[3] <= 0 or box[0] + box[2] > 1 + 1e-9 or box[1] + box[3] > 1 + 1e-9:
            _fail("Saved detection box is outside the image")
        _number(item.get("confidence"), "detection confidence", minimum=0, maximum=1)
        _integer(item.get("track_id"), 1, 2**53 - 1, "local track ID")


def _summary(report, rows):
    """Validate displayed saved scores and bind their denominators to saved rows."""
    summaries = _object(report.get("summary_by_split"), "saved summaries")
    splits = {row["split"] for row in rows}
    if set(summaries) != splits:
        _fail("Summary splits differ from saved comparison frames")
    for split, models in summaries.items():
        subset = [row for row in rows if row["split"] == split]
        if not isinstance(models, dict) or set(models) != set(MODELS):
            _fail("Summary must contain baseline and candidate")
        for name, summary in models.items():
            _object(summary, "model summary")
            if summary.get("frames") != len(subset):
                _fail("Summary frame count differs from saved rows")
            detections = sum(len(row["models"][name]["detections"]) for row in subset)
            if summary.get("detections") != detections:
                _fail("Summary detection count differs from saved rows")
            ref = _object(summary.get("reference"), "reference summary")
            labeled = [r for r in subset if r.get("boxes") is not None]
            if ref.get("labeled_frames") != len(labeled):
                _fail("Summary labeled-frame count differs from source references")
            for key in ("tp", "fp", "fn"):
                _integer(ref.get(key), 0, MAX_ROWS * 1000, "reference " + key)
            if not labeled and any(ref[key] for key in ("tp", "fp", "fn")):
                _fail("Unannotated images cannot carry reference scores")
            expected_truth = sum(sum(b["label"].strip().casefold() == "person" for b in r["boxes"]) for r in labeled)
            expected_predictions = sum(len(r["models"][name]["detections"]) for r in labeled)
            if ref["tp"] + ref["fn"] != expected_truth or ref["tp"] + ref["fp"] != expected_predictions:
                _fail("Reference scores disagree with annotated-frame counts")
            for metric, denominator in (("precision", ref["tp"] + ref["fp"]),
                                         ("recall", ref["tp"] + ref["fn"])):
                expected = ref["tp"] / denominator if labeled and denominator else None
                if ref.get(metric) != expected:
                    _fail("Inconsistent saved reference ratio")
            if ref.get("iou_threshold") != .5 or ref.get("target_identity_metrics") is not None:
                _fail("Unsupported or unsupported-identity reference score")
            latency = _object(summary.get("latency_ms"), "latency summary")
            for metric in ("inference_ms", "processing_ms"):
                values = _object(latency.get(metric), metric)
                for key in ("median", "max"):
                    _number(values.get(key), "latency " + key, minimum=0)
    return summaries


def _image(data, width, height):
    try:
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG"} or image.size != (width, height):
                _fail("Source image format/dimensions differ from saved evidence")
            mime = "image/jpeg" if image.format == "JPEG" else "image/png"
            image.verify()
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    except (OSError, ValueError) as exc:
        if isinstance(exc, ComparisonExportError):
            raise
        raise ComparisonExportError("Cannot validate original source image") from exc


def load_comparison(directory):
    """Read completed generic-manifest comparisons and verify every image binding."""
    directory = Path(directory).expanduser().resolve()
    raw, report_file = _read_input(directory / "report.json", 4 * 1024 * 1024)
    report = _object(_json(raw, "report.json"), "report")
    if (report.get("format") != "argos.custom-vision-comparison"
            or type(report.get("version")) is not int or report["version"] != 1
            or report.get("state") != "complete"):
        _fail("A completed custom vision comparison is required")
    source = _object(report.get("source"), "source")
    if source.get("kind") != "frames_manifest":
        _fail("This viewer requires a generic frames-manifest comparison")
    manifest_path = Path(_text(source.get("path"), "source manifest path")).expanduser()
    if not manifest_path.is_absolute():
        _fail("Recorded source manifest path must be absolute")
    manifest_raw, manifest_file = _read_input(manifest_path, 32 * 1024 * 1024)
    if manifest_file.sha256 != _hex(source.get("sha256"), 64, "source manifest hash"):
        _fail("Source manifest changed since comparison")
    manifest = _object(_json(manifest_raw, "source manifest"), "source manifest")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        _fail("Unsupported source manifest version")
    source_frames = manifest.get("frames")
    if not isinstance(source_frames, list):
        _fail("Source manifest needs frames")
    models = _object(report.get("models"), "model provenance")
    if set(models) != set(MODELS):
        _fail("Report needs baseline and candidate model provenance")
    for name in MODELS:
        model = _object(models[name], "model provenance")
        _hex(model.get("sha256"), 64, "model hash")
    frames_raw, frames_file = _read_input(directory / "frames.jsonl", 64 * 1024 * 1024)
    lines = frames_raw.splitlines()
    if not 1 <= len(lines) <= MAX_ROWS or source.get("selected_frames") != len(lines):
        _fail("Completed comparison frame count is inconsistent")
    files = [report_file, frames_file, manifest_file]
    rows, embedded, dimensions, images_bytes, previous = [], {}, {}, 0, -1
    for line in lines:
        if len(line) > 1024 * 1024:
            _fail("Saved frame row exceeds 1 MiB")
        row = _object(_json(line, "frame row"), "frame row")
        index = _integer(row.get("index"), previous + 1, len(source_frames) - 1, "source index")
        previous = index
        original = _object(source_frames[index], "source frame")
        for field in ("id", "source_id", "clip_id", "captured_at", "width", "height",
                      "sha256", "split", "scene_group", "boxes", "reference_status"):
            if row.get(field) != original.get(field):
                _fail(f"Comparison row differs from source manifest: {field}")
        for field in ("id", "source_id", "split", "scene_group"):
            _text(row.get(field), "frame " + field)
        for field in ("width", "height"):
            _integer(row.get(field), 1, 4096, field)
        _integer(row.get("segment"), 0, MAX_ROWS, "segment")
        at = _number(row.get("captured_at"), "receipt timestamp", minimum=0)
        if row.get("received_at") != at:
            _fail("Tracker timestamp differs from source timestamp")
        digest = _hex(row.get("sha256"), 64, "image hash")
        if row.get("jpeg_sha256") != digest:
            _fail("Comparison image hash differs from source image")
        if not isinstance(row.get("models"), dict) or set(row["models"]) != set(MODELS):
            _fail("Every image needs both saved model results")
        for result in row["models"].values():
            _object(result, "model result")
            _detections(result.get("detections"))
            for metric in ("inference_ms", "processing_ms"):
                _number(result.get(metric), metric, minimum=0)
        boxes = row.get("boxes")
        if boxes is not None:
            if row.get("reference_status") != "human_validated" or not isinstance(boxes, list):
                _fail("Reference boxes require human validation")
            for box in boxes:
                _object(box, "reference box")
                _text(box.get("label"), "reference label")
                coords = box.get("box")
                if not isinstance(coords, list) or len(coords) != 4:
                    _fail("Reference needs xyxy pixels")
                for position, number in enumerate(coords):
                    _number(number, "reference coordinate", minimum=0,
                            maximum=row["width"] if position % 2 == 0 else row["height"])
                if coords[2] <= coords[0] or coords[3] <= coords[1]:
                    _fail("Reference box has empty area")
        image_path = Path(_text(original.get("path"), "source image path")).expanduser()
        if not image_path.is_absolute():
            image_path = manifest_path.parent / image_path
        data, image_file = _read_input(image_path, 16 * 1024 * 1024)
        if image_file.sha256 != digest:
            _fail("Original source image failed its SHA-256 check")
        files.append(image_file)
        if digest in dimensions and dimensions[digest] != (row["width"], row["height"]):
            _fail("One original image has conflicting dimensions")
        dimensions[digest] = row["width"], row["height"]
        if digest not in embedded:
            embedded[digest] = _image(data, row["width"], row["height"])
            images_bytes += len(embedded[digest])
            if images_bytes > MAX_HTML_BYTES - 1024 * 1024:
                _fail("Original images exceed the 64 MiB offline viewer budget; no images were sampled or reencoded")
        rows.append(row)
    summary = _summary(report, rows)
    return {"report": report, "rows": rows, "images": embedded, "summary": summary,
            "files": files, "directory": directory, "source_directory": manifest_path.parent}


def render_comparison(comparison):
    frames = [{key: row.get(key) for key in (
        "id", "clip_id", "captured_at", "split", "scene_group", "width", "height",
        "sha256", "segment", "reference_status", "boxes", "models")}
        for row in comparison["rows"]]
    data = {"frames": frames, "images": comparison["images"],
            "summary": comparison["summary"], "models": comparison["report"]["models"],
            "source_sha256": comparison["report"]["source"]["sha256"],
            "limits": comparison["report"].get("limits", [])}
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False)
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = TEMPLATE.read_text(encoding="utf-8")
    script = template.split('<script id="viewer-script">', 1)[1].split("</script>", 1)[0]
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    html = template.replace("__SCRIPT_HASH__", script_hash).replace("__COMPARISON_DATA__", payload)
    encoded = html.encode("utf-8")
    if len(encoded) > MAX_HTML_BYTES:
        _fail("Offline viewer exceeds 64 MiB; all original frames are retained or export fails")
    return encoded


def export_comparison(directory, output_directory):
    comparison = load_comparison(directory)
    output = Path(output_directory).expanduser().absolute()
    for source in (comparison["directory"], comparison["source_directory"]):
        resolved = output.resolve()
        if resolved == source or source in resolved.parents:
            _fail("Keep viewer output outside comparison and source directories")
    if output.exists():
        _fail("Viewer output must be a new directory; existing paths are never overwritten")
    html = render_comparison(comparison)
    for file in comparison["files"]:
        file.verify()
    output.mkdir(parents=True, exist_ok=False)
    # Completion receipt is published last. Interrupted exports keep .partial.
    temporary = output / "index.html.partial"
    with temporary.open("xb") as stream:
        stream.write(html)
    receipt = {"format": "argos.custom-vision-viewer", "version": 1, "state": "complete",
        "frames": len(comparison["rows"]), "unique_images": len(comparison["images"]),
        "all_comparison_frames_included": True, "image_bytes": "original, no reencoding",
        "html_bytes": len(html), "html_sha256": hashlib.sha256(html).hexdigest(),
        "comparison_report_sha256": comparison["files"][0].sha256,
        "comparison_frames_sha256": comparison["files"][1].sha256,
        "source_manifest_sha256": comparison["files"][2].sha256,
        "inference_performed": False}
    temporary.rename(output / "index.html")
    with (output / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return receipt
