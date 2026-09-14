#!/usr/bin/env python3
"""Export an existing Tiny/S comparison as a portable, local visual report.

Reuses saved results and verifies their exact source JPEGs; performs no inference
and opens no camera, console, simulator or command transport. Open index.html
directly in a browser, keeping its images directory alongside it.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO))

from argos.perception.comparison_export import load_comparison


PLACEHOLDER = "__ARGOS_COMPARISON_DATA__"


def render_html(data):
    """Embed data as JSON, never as executable JavaScript or unescaped HTML."""
    template = files("argos.perception").joinpath("static/vision_comparison.html").read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise ValueError("The visual comparison template must contain one data placeholder")
    payload = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    for character, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        payload = payload.replace(character, escaped)
    return template.replace(PLACEHOLDER, payload), hashlib.sha256(template.encode()).hexdigest()


def create_output(directory, forbidden):
    forbidden = [Path(path).resolve() for path in forbidden]
    if directory is None:
        root = Path(tempfile.gettempdir()).resolve()
        if any(root == path or path in root.parents for path in forbidden):
            raise ValueError("Specify output outside the comparison and source recordings directories")
        return Path(tempfile.mkdtemp(prefix="argos-vision-view-"))
    directory = Path(directory).expanduser().resolve()
    if any(directory == path or path in directory.parents for path in forbidden):
        raise ValueError("Keep output outside the comparison and source recordings directories")
    if directory.exists():
        if not directory.is_dir() or any(directory.iterdir()):
            raise ValueError("Output directory must be new or empty")
    else:
        directory.mkdir(parents=True)
    return directory


def write_manifest(directory, manifest):
    temporary = directory / "manifest.json.partial"
    temporary.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(directory / "manifest.json")


def export_view(comparison_dir, output_dir=None, *, recordings_dir=None):
    source = load_comparison(comparison_dir, recordings_dir)
    output = create_output(output_dir, [source.comparison_directory, source.source_directory])
    manifest = {"format": "argos.vision-comparison-view", "version": 1,
                "state": "building", "inference_performed": False,
                "source": {"id": source.report["source"]["id"],
                           "journal_revision": source.journal_revision,
                           "visual_revision": source.visual_revision},
                "comparison_sha256": source.input_sha256,
                "exporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "validator_sha256": hashlib.sha256(files("argos.perception").joinpath(
                    "comparison_export.py").read_bytes()).hexdigest(),
                "images": []}
    try:
        write_manifest(output, manifest)
        (output / "images").mkdir()
        frames = []
        for position, row in enumerate(source.rows):
            jpeg = source.read_frame(row)
            relative = f"images/{position:06d}.jpg"
            with (output / relative).open("xb") as image:
                image.write(jpeg)
            frame = {key: row[key] for key in ("index", "at_s", "available_at_s", "segment",
                     "sequence", "video_id", "width", "height", "jpeg_sha256")}
            frame["excluded_before"] = row.get("excluded_before", False)
            frame["models"] = {variant: {
                "detections": [{key: detection[key] for key in ("box", "confidence", "track_id")}
                               for detection in row["models"][variant]["detections"]],
                "inference_ms": row["models"][variant]["inference_ms"],
                "processing_ms": row["models"][variant]["processing_ms"],
            } for variant in ("tiny", "s")}
            frames.append({**frame, "image": relative})
            manifest["images"].append({"path": relative, "archive_index": row["index"],
                                      "sha256": hashlib.sha256(jpeg).hexdigest(), "bytes": len(jpeg)})
        report = source.report
        # Only export relevant public display fields; original local source/model
        # paths and comparison machine details stay in the existing JSON report.
        data = {"format": manifest["format"], "version": 1,
                "source": {key: report["source"][key] for key in (
                    "id", "environment", "duration_s", "selected_frames", "archive_frame_rows",
                    "truncated", "skipped_counts")},
                "models": {variant: {key: report["models"][variant][key]
                            for key in ("label", "input_size")} for variant in ("tiny", "s")},
                "summary": report["summary"], "frames": frames,
                "differences": [position for position, row in enumerate(source.rows)
                    if len(row["models"]["tiny"]["detections"]) != len(row["models"]["s"]["detections"])],
                "export": {"binding": "archive and frame hashes verified",
                           "comparison_sha256": source.input_sha256}}
        html, template_sha = render_html(data)
        (output / "index.html.partial").write_text(html, encoding="utf-8")
        source.verify_unchanged()
        manifest.update(state="complete", frames=len(frames),
                        count_disagreements=len(data["differences"]), template_sha256=template_sha,
                        html_sha256=hashlib.sha256(html.encode()).hexdigest())
        # A view becomes openable only after all images and input bindings pass.
        (output / "index.html.partial").replace(output / "index.html")
        # Publish the completion marker last: an abrupt stop must not report a
        # complete export before the final HTML exists.
        write_manifest(output, manifest)
        return output
    except (Exception, KeyboardInterrupt) as exc:
        manifest.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                        error=f"{type(exc).__name__}: {exc}")
        try:
            if (output / "index.html").exists():
                (output / "index.html").replace(output / "index.html.partial")
            write_manifest(output, manifest)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-dir", type=Path, required=True,
                        help="completed comparison directory containing report.json and frames.jsonl")
    parser.add_argument("--recordings-dir", type=Path,
                        help="relocated native archives; must match the saved comparison revisions")
    parser.add_argument("--output-dir", type=Path,
                        help="new/empty directory outside the inputs; defaults to a fresh temporary directory")
    args = parser.parse_args(argv)
    try:
        output = export_view(args.comparison_dir, args.output_dir, recordings_dir=args.recordings_dir)
    except KeyboardInterrupt:
        print("Visual export interrupted; any generated files remain incomplete.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Visual export failed: {exc}", file=sys.stderr)
        return 1
    print(f"Open {output / 'index.html'} in a browser. Keep the images directory beside it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
