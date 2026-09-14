#!/usr/bin/env python3
"""Recompute Tiny/S image detections and tracking on one native ARGOS capture.

All processing is offline. No console, camera, simulation or command transport
is opened. Archived detections are never used as detector inputs or truth labels.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO))

from argos.console.archive import RecordingArchive
from argos.console.visual_recording import VisualArchive
from argos.perception.appearance import AppearanceEncoder
from argos.perception.image_tracks import ImageTracker
from argos.perception.yolox import (
    MODEL_CATALOG, YoloXPersonDetector, default_model_path, validate_inference_threads,
)


VARIANTS = ("tiny", "s")
WARMUP_FRAMES = 2


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class ArchiveImages:
    """Keep each unique chronological JPEG once, with recorded receipt times.

    Archive availability order can contain a raw image followed by its analyzed
    duplicate or an older completed analysis. Such rows are counted explicitly;
    they must not create extra detections or rewrite the tracker's clock.
    """

    def __init__(self, directory, identifier, max_frames):
        self.directory = Path(directory).resolve()
        self.identifier = identifier
        if type(max_frames) is not int or not 1 <= max_frames <= 5000:
            raise ValueError("max_frames must be between 1 and 5000")
        self.limit = max_frames
        self.telemetry_archive = RecordingArchive(self.directory)
        self.telemetry = self.telemetry_archive.metadata(identifier)
        context = self.telemetry.get("context")
        if not context or not context.get("run_id"):
            raise ValueError("A native capture with a recorded run ID is required")
        self.binding = {"started_at": self.telemetry["started_at"], "run_id": context["run_id"]}
        self.archive = VisualArchive(self.directory)
        self.visual = self.archive.metadata(identifier, **self.binding)
        if self.visual.get("state") != "complete" or self.visual.get("frames", 0) < 1:
            raise ValueError("A completed visual capture containing JPEGs is required")
        if self.visual["ended_at"] > self.telemetry["ended_at"]:
            raise ValueError("Visual capture ends after its telemetry journal")
        self.access = {**self.binding, "revision": self.visual["revision"]}
        self.seen = {}
        self.context = None
        self.last_received = None
        self.segment = -1
        self.selected = self.examined = 0
        self.skipped_counts = Counter()
        self.skipped = []
        self.excluded_before = False

    def __iter__(self):
        for index in range(self.visual["frames"]):
            if self.selected >= self.limit:
                break
            frame = self.archive.frame_record(self.identifier, index, **self.access)
            self.examined += 1
            digest = hashlib.sha256(frame["jpeg"]).hexdigest()
            identity = (frame["video_id"], frame["sequence"], frame["received_at"],
                        frame["width"], frame["height"])
            reason = None
            if identity in self.seen:
                if self.seen[identity] != digest:
                    raise ValueError("One archived image identity has conflicting JPEG bytes")
                reason = "duplicate_image"
            else:
                self.seen[identity] = digest
            context = (frame["video_id"], frame["width"], frame["height"])
            if (reason is None and context == self.context and self.last_received is not None
                    and frame["received_at"] <= self.last_received):
                reason = "non_increasing_receipt"
            if reason:
                self.skipped_counts[reason] += 1
                self.skipped.append({"index": index, "reason": reason})
                if reason == "non_increasing_receipt":
                    self.excluded_before = True
                continue
            reset = context != self.context
            if reset:
                self.segment += 1
            self.context, self.last_received = context, frame["received_at"]
            self.selected += 1
            excluded = self.excluded_before
            self.excluded_before = False
            yield {**frame, "jpeg_sha256": digest, "segment": self.segment,
                   "excluded_before": excluded,
                   "reset_tracker": reset,
                   "at_s": frame["received_at"] - self.telemetry["started_at"],
                   "available_at_s": frame["available_at"] - self.telemetry["started_at"]}

    def verify_unchanged(self):
        current = self.telemetry_archive.metadata(self.identifier)
        visual = self.archive.metadata(self.identifier, **self.binding)
        if (current["revision"] != self.telemetry["revision"]
                or visual.get("revision") != self.visual["revision"]):
            raise ValueError("Source capture changed during evaluation")

    def description(self):
        configuration = self.telemetry["context"]["configuration"]
        return {"id": self.identifier, "directory": str(self.directory),
                "environment": configuration["environment"],
                "video_source": configuration["video_source"],
                "duration_s": self.telemetry["duration_s"],
                "telemetry_revision": self.telemetry["revision"],
                "visual_revision": self.visual["revision"],
                "archive_frame_rows": self.visual["frames"],
                "examined_frame_rows": self.examined, "selected_frames": self.selected,
                "skipped_counts": dict(self.skipped_counts),
                "truncated": self.examined < self.visual["frames"],
                "unevaluated_frame_rows": self.visual["frames"] - self.examined,
                "visual_recording_dropped": self.visual["dropped"]}


class Pipeline:
    """The production detector, appearance encoder and tracker, with no flight law."""

    def __init__(self, model_path, variant, threads):
        self.detector = YoloXPersonDetector(model_path, variant=variant, threads=threads)
        self.encoder = AppearanceEncoder()
        self.tracker = ImageTracker()

    def warmup(self, jpeg):
        for _ in range(WARMUP_FRAMES):
            result = self.detector.detect(jpeg)
            self.encoder.encode(jpeg, result["detections"],
                                width=result["width"], height=result["height"])

    def process(self, frame):
        if frame["reset_tracker"]:
            self.tracker.reset()
        started = time.perf_counter()
        result = self.detector.detect(frame["jpeg"])
        if (result["width"], result["height"]) != (frame["width"], frame["height"]):
            raise ValueError("Detector dimensions differ from the archived JPEG")
        appearances = self.encoder.encode(frame["jpeg"], result["detections"],
                                         width=result["width"], height=result["height"])
        detections = self.tracker.update(result["detections"], frame["received_at"],
                                         appearances=appearances)
        return {"detections": detections, "inference_ms": result["inference_ms"],
                "processing_ms": (time.perf_counter() - started) * 1000}


def evaluate(images, pipelines, output):
    rows = []
    for frame in images:
        if not rows:
            for pipeline in pipelines.values():
                pipeline.warmup(frame["jpeg"])
        # Serial inference avoids model contention; alternating order reduces
        # fixed first/second bias. This remains one pass, not a CPU benchmark.
        order = VARIANTS if len(rows) % 2 == 0 else tuple(reversed(VARIANTS))
        results = {variant: pipelines[variant].process(frame) for variant in order}
        row = {key: value for key, value in frame.items() if key not in {"jpeg", "reset_tracker"}}
        row["models"] = {variant: results[variant] for variant in VARIANTS}
        output.write(json.dumps(row, allow_nan=False) + "\n")
        output.flush()
        rows.append(row)
        if len(rows) % 25 == 0:
            print(f"Compared {len(rows)} images", flush=True)
    if not rows:
        raise ValueError("No chronological images were available for comparison")
    images.verify_unchanged()
    return rows


def provenance():
    versions = {}
    for package in ("argos", "numpy", "opencv-python-headless", "pillow"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    sources = [Path(__file__).resolve(), REPO / "argos/perception/yolox.py",
               REPO / "argos/perception/image_tracks.py", REPO / "argos/perception/appearance.py",
               REPO / "argos/perception/evaluation_report.py", REPO / "argos/console/visual_recording.py"]
    result = {"python": platform.python_version(), "platform": platform.platform(),
              "cpu_count": os.cpu_count(), "packages": versions,
              "source_sha256": {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sources}}
    for key, command in (("git_head", ["git", "rev-parse", "HEAD"]),
                         ("git_status", ["git", "status", "--porcelain"])):
        try:
            result[key] = subprocess.check_output(command, cwd=REPO, text=True,
                stderr=subprocess.DEVNULL, timeout=3).strip()
        except (OSError, subprocess.SubprocessError):
            result[key] = None
    return result


def create_output(directory, source):
    if directory is None:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if temporary_root == source or source in temporary_root.parents:
            raise ValueError("Specify an output directory outside the source recordings directory")
        return Path(tempfile.mkdtemp(prefix="argos-vision-comparison-"))
    directory = Path(directory).expanduser().resolve()
    if directory == source or source in directory.parents:
        raise ValueError("Keep comparison output outside the source recordings directory")
    if directory.exists():
        if not directory.is_dir() or any(directory.iterdir()):
            raise ValueError("Output directory must be new or empty")
    else:
        directory.mkdir(parents=True)
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recordings-dir", type=Path, required=True)
    parser.add_argument("--recording", required=True, help="native recording ID (32 hex characters)")
    parser.add_argument("--tiny-model", type=Path, default=default_model_path("tiny"))
    parser.add_argument("--s-model", type=Path, default=default_model_path("s"))
    parser.add_argument("--threads", type=int, default=2, help="same CPU thread limit for both models, 1..6")
    parser.add_argument("--max-frames", type=int, default=1000, help="maximum unique chronological images, 1..5000")
    parser.add_argument("--output-dir", type=Path, help="new/empty directory; defaults to a fresh temporary directory")
    args = parser.parse_args(argv)
    output_dir = images = None
    report = {"format": "argos.vision-comparison", "version": 1, "state": "running"}
    try:
        from argos.perception.evaluation_report import render_markdown, summarize
        validate_inference_threads(args.threads)
        images = ArchiveImages(args.recordings_dir, args.recording, args.max_frames)
        output_dir = create_output(args.output_dir, images.directory)
        print(f"Comparison output: {output_dir}", flush=True)
        paths = {"tiny": args.tiny_model, "s": args.s_model}
        report.update(source=images.description(), provenance=provenance(),
            configuration={"threads": args.threads, "warmup_frames": WARMUP_FRAMES,
                           "max_frames": args.max_frames, "timed_passes": 1,
                           "inference_order": "serial, alternate first model on each image"},
            models={variant: {"label": MODEL_CATALOG[variant].label,
                    "input_size": MODEL_CATALOG[variant].input_size,
                    "sha256": MODEL_CATALOG[variant].sha256,
                    "path": str(paths[variant].expanduser().resolve())} for variant in VARIANTS})
        write_json(output_dir / "report.json", report)
        pipelines = {variant: Pipeline(paths[variant], variant, args.threads) for variant in VARIANTS}
        with (output_dir / "frames.jsonl.partial").open("x", encoding="utf-8") as output:
            rows = evaluate(images, pipelines, output)
        report.update(state="complete", source=images.description(), summary=summarize(rows),
            skipped_rows=images.skipped,
            limits=[
                "No truth labels: detection counts and display ID changes are not accuracy, recall or identity-switch scores.",
                "Track IDs are local to each model run; matching numeric IDs across models proves nothing.",
                "Only saved JPEGs are evaluated; archive sampling, recompression and missing frames limit conclusions.",
                "Receipt times drive tracking; availability delays and the live frame-selection/deadline policy are not replayed.",
                "Inference time measures the network only; processing time includes decode, detection, appearance and tracking, excluding archive I/O and model loading.",
                "Two warmups per model are excluded from timing and tracking. One serial pass does not establish live throughput or end-to-end camera latency.",
                "More detections or fewer IDs do not automatically mean a better result. Review the corresponding source images.",
            ])
        markdown = render_markdown(report)
        (output_dir / "report.md.partial").write_text(markdown, encoding="utf-8")
        (output_dir / "frames.jsonl.partial").rename(output_dir / "frames.jsonl")
        (output_dir / "report.md.partial").rename(output_dir / "report.md")
        write_json(output_dir / "report.json", report)
        print(f"Compared {len(rows)} images. Read {output_dir / 'report.md'}", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        report.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                      error=f"{type(exc).__name__}: {exc}")
        if images is not None:
            report["source"] = images.description()
        if output_dir is not None:
            for name in ("report.md", "frames.jsonl"):
                path = output_dir / name
                if path.exists():
                    try:
                        path.replace(output_dir / (name + ".partial"))
                    except OSError:
                        pass
            try:
                write_json(output_dir / "report.json", report)
            except OSError as error:
                print(f"Unable to save failure report: {error}", file=sys.stderr)
        print(f"Comparison {report['state']}: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
