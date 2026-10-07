#!/usr/bin/env python3
"""Compare pinned Nano with an explicit custom bundle offline, without devices.

Uses the production person detector, appearance encoder and image tracker.
Native archives or hash-checked frame manifests are read without modification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO))

from examples.compare_vision import ArchiveImages, Pipeline, create_output, provenance, write_json
from argos.perception.appearance import AppearanceEncoder
from argos.perception.image_tracks import ImageTracker
from argos.perception.model_bundle import read_model_bundle
from argos.perception.yolox import (CONFIDENCE_THRESHOLD, NMS_THRESHOLD, YoloXPersonDetector,
                                    default_model_path, get_model_spec, validate_inference_threads)

MODELS = ("baseline", "candidate")


class ManifestImages:
    """Original order/times, explicit clip boundaries and per-image byte integrity.

Missing boxes are unknown, never negatives. Isolated photos reset the tracker
on every image; only explicitly grouped clips can yield continuity observations.
"""
    def __init__(self, path, max_frames=1000, split=None):
        self.path = Path(path).expanduser().resolve()
        self.directory = self.path.parent
        raw = self.path.read_bytes()
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("frames manifest exceeds 32 MiB")
        self.digest = hashlib.sha256(raw).hexdigest()
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1 or not isinstance(value.get("frames"), list)):
            raise ValueError("frames manifest requires schema_version 1 and a frames list")
        if type(max_frames) is not int or not 1 <= max_frames <= 5000:
            raise ValueError("max_frames must be between 1 and 5000")
        self.frames, self.limit, self.split = value["frames"], max_frames, split
        self.selected = self.examined = 0
        self.skipped = []
        self.verified = []

    def __iter__(self):
        identifiers, clip_times = set(), {}
        context, segment, first = None, -1, None
        for index, item in enumerate(self.frames):
            if self.selected >= self.limit:
                break
            self.examined += 1
            if not isinstance(item, dict):
                raise ValueError("manifest frame must be an object")
            if self.split is not None and item.get("split") != self.split:
                self.skipped.append({"index": index, "reason": "split_filter"})
                continue
            for field in ("id", "source_id", "path", "scene_group", "split"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise ValueError(f"manifest frame requires {field}")
            if item["id"] in identifiers:
                raise ValueError("manifest frame IDs must be unique")
            identifiers.add(item["id"])
            at = item.get("captured_at")
            if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
                raise ValueError("manifest timestamps must be finite nonnegative seconds")
            if any(type(item.get(k)) is not int or not 1 <= item[k] <= 4096
                   for k in ("width", "height")):
                raise ValueError("manifest image dimensions must be in 1..4096")
            clip = item.get("clip_id")
            if clip is not None and (not isinstance(clip, str) or not clip):
                raise ValueError("clip_id must be a nonempty string")
            current = (item["source_id"], clip or item["id"], item["scene_group"],
                       item["split"], item["width"], item["height"])
            if clip is not None:
                if current in clip_times and at <= clip_times[current]:
                    raise ValueError("clip timestamps must increase; frames are never silently reordered")
                clip_times[current] = at
            reset = current != context or clip is None
            if reset:
                segment += 1
                first = at
            context = current
            source = Path(item["path"]).expanduser()
            if not source.is_absolute():
                source = self.directory / source
            source = source.resolve()
            with source.open("rb") as stream:
                data = stream.read(16 * 1024 * 1024 + 1)
            digest = hashlib.sha256(data).hexdigest()
            if len(data) > 16 * 1024 * 1024 or digest != item.get("sha256"):
                raise ValueError("manifest image failed its size/SHA-256 check")
            boxes = item.get("boxes")
            if boxes is not None:
                if item.get("reference_status") != "human_validated" or not isinstance(boxes, list):
                    raise ValueError("reference boxes need explicit human_validated status")
                for box in boxes:
                    coords = box.get("box") if isinstance(box, dict) else None
                    if (not isinstance(coords, list) or len(coords) != 4
                            or any(type(v) not in (int, float) or not math.isfinite(v) for v in coords)
                            or not 0 <= coords[0] < coords[2] <= item["width"]
                            or not 0 <= coords[1] < coords[3] <= item["height"]
                            or not isinstance(box.get("label"), str)):
                        raise ValueError("reference boxes must be labeled finite xyxy pixels inside image")
            self.verified.append((source, digest))
            self.selected += 1
            yield {**item, "index": index, "jpeg": data, "jpeg_sha256": digest,
                   "segment": segment, "reset_tracker": reset, "excluded_before": False,
                   "video_id": item["source_id"], "sequence": item.get("source_sequence", index),
                   "received_at": at, "available_at": at, "at_s": at - first,
                   "available_at_s": at - first, "continuity_observable": clip is not None}

    def verify_unchanged(self):
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.digest:
            raise ValueError("frames manifest changed during comparison")
        for path, digest in self.verified:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("source image changed during comparison")

    def description(self):
        return {"kind": "frames_manifest", "path": str(self.path), "sha256": self.digest,
                "frames": len(self.frames), "selected_frames": self.selected,
                "examined_frames": self.examined, "split_filter": self.split,
                "truncated": self.examined < len(self.frames), "skipped": self.skipped}


class ComparisonPipeline(Pipeline):
    """Use the current live worker's single-decode detector/appearance path."""
    def warmup(self, jpeg):
        for _ in range(2):
            image = self.detector.decode_jpeg(jpeg)
            result = self.detector.detect_bgr(image)
            self.encoder.encode_bgr(image, result["detections"],
                                    width=result["width"], height=result["height"])

    def process(self, frame):
        if frame["reset_tracker"]:
            self.tracker.reset()
        started = time.perf_counter()
        image = self.detector.decode_jpeg(frame["jpeg"])
        result = self.detector.detect_bgr(image)
        if (result["width"], result["height"]) != (frame["width"], frame["height"]):
            raise ValueError("detector dimensions differ from the source frame")
        appearances = self.encoder.encode_bgr(image, result["detections"],
                                             width=result["width"], height=result["height"])
        detections = self.tracker.update(result["detections"], frame["received_at"],
                                         appearances=appearances)
        return {"detections": detections, "inference_ms": result["inference_ms"],
                "processing_ms": (time.perf_counter() - started) * 1000}


class CustomPipeline(ComparisonPipeline):
    def __init__(self, bundle, threads):
        self.detector = YoloXPersonDetector(bundle_path=bundle, threads=threads)
        self.encoder, self.tracker = AppearanceEncoder(), ImageTracker()


def evaluate(images, pipelines, output):
    rows = []
    for frame in images:
        if not rows:
            for pipeline in pipelines.values():
                pipeline.warmup(frame["jpeg"])
        order = MODELS if len(rows) % 2 == 0 else tuple(reversed(MODELS))
        results = {name: pipelines[name].process(frame) for name in order}
        row = {k: v for k, v in frame.items() if k not in {"jpeg", "reset_tracker"}}
        row.setdefault("continuity_observable", True)  # Native archive frames retain their timeline.
        row["models"] = {name: results[name] for name in MODELS}
        output.write(json.dumps(row, allow_nan=False) + "\n")
        output.flush()
        rows.append(row)
    if not rows:
        raise ValueError("no frames selected for comparison")
    images.verify_unchanged()
    return rows


def _iou(a, b):
    intersection = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    union = ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection)
    return intersection / union if union else 0.


def summarize(rows):
    result = {}
    for split in sorted({row.get("split", "unspecified") for row in rows}):
        subset = [row for row in rows if row.get("split", "unspecified") == split]
        models = {}
        for name in MODELS:
            tp = fp = fn = labeled = 0
            ids = set()
            gaps, active_gap, context = [], 0, None
            positive_frames = 0
            for row in subset:
                detections = row["models"][name]["detections"]
                positive_frames += bool(detections)
                if row.get("continuity_observable", False):
                    if context != row["segment"]:
                        if active_gap:
                            gaps.append(active_gap)
                        active_gap = 0
                        context = row["segment"]
                    ids.update((row["segment"], d["track_id"]) for d in detections)
                    if detections:
                        if active_gap:
                            gaps.append(active_gap)
                        active_gap = 0
                    else:
                        active_gap += 1
                truth = row.get("boxes")
                if truth is None:
                    continue
                labeled += 1
                truth = [b["box"] for b in truth if b["label"].strip().casefold() == "person"]
                unmatched = set(range(len(truth)))
                for detection in sorted(detections, key=lambda d: -d["confidence"]):
                    x, y, w, h = detection["box"]
                    pixels = [x * row["width"], y * row["height"],
                              (x + w) * row["width"], (y + h) * row["height"]]
                    scores = [(_iou(pixels, truth[i]), i) for i in sorted(unmatched)]
                    best = max(scores, default=(0., -1))
                    if best[0] >= .5:
                        tp += 1
                        unmatched.remove(best[1])
                    else:
                        fp += 1
                fn += len(unmatched)
            if active_gap:
                gaps.append(active_gap)
            models[name] = {
                "frames": len(subset), "frames_with_detections": positive_frames,
                "detections": sum(len(r["models"][name]["detections"]) for r in subset),
                "reference": {"labeled_frames": labeled, "tp": tp, "fp": fp, "fn": fn,
                    "precision": tp / (tp + fp) if labeled and tp + fp else None,
                    "recall": tp / (tp + fn) if labeled and tp + fn else None,
                    "iou_threshold": .5, "target_identity_metrics": None},
                "continuity_observations": {"clip_local_track_ids": len(ids),
                    "empty_detection_runs_frames": gaps,
                    "meaning": "Detection availability, not target losses or true identity switches"},
                "latency_ms": {metric: {"median": statistics.median(r["models"][name][metric] for r in subset),
                    "max": max(r["models"][name][metric] for r in subset)}
                    for metric in ("inference_ms", "processing_ms")}}
        result[split] = models
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames-manifest", type=Path)
    parser.add_argument("--recordings-dir", type=Path)
    parser.add_argument("--recording")
    parser.add_argument("--split", choices=("train", "val", "test"))
    parser.add_argument("--baseline-model", type=Path, default=default_model_path("nano"))
    parser.add_argument("--vision-bundle", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--max-frames", type=int, default=1000)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    report = {"format": "argos.custom-vision-comparison", "version": 1, "state": "running"}
    output = images = None
    try:
        validate_inference_threads(args.threads)
        if args.frames_manifest:
            if args.recordings_dir or args.recording:
                raise ValueError("choose frames manifest or native recording, not both")
            images = ManifestImages(args.frames_manifest, args.max_frames, args.split)
        else:
            if not args.recordings_dir or not args.recording or args.split:
                raise ValueError("specify --frames-manifest or --recordings-dir and --recording")
            images = ArchiveImages(args.recordings_dir, args.recording, args.max_frames)
        output = create_output(args.output_dir, images.directory)
        bundle = read_model_bundle(args.vision_bundle)
        baseline = get_model_spec("nano")
        report.update(source=images.description(), provenance=provenance(),
            models={"baseline": {"variant": "nano", "sha256": baseline.sha256},
                    "candidate": bundle.identity()},
            configuration={"threads": args.threads, "confidence_threshold": CONFIDENCE_THRESHOLD,
                "nms_threshold": NMS_THRESHOLD, "warmup_frames_per_model": 2,
                "inference_order": "serial, alternate model order on each image",
                "timed_passes": 1},
            limits=["Only human_validated frames contribute reference quality; unknown boxes are not negatives.",
                "Training and validation are reported separately. Existing validation may have informed earlier experiments.",
                "No target identity labels: empty detections and local track counts are not true target-loss or identity-switch rates.",
                "Original host receipt timestamps are preserved; live frame selection and command deadlines are not replayed.",
                "Timings cover this host and one serial pass, not target-laptop throughput, camera latency or flight improvement.",
                "No model promotion, control transport, service restart or hardware access."])
        for path in (Path(__file__).resolve(), REPO / "argos/perception/model_bundle.py"):
            report["provenance"]["source_sha256"][str(path.relative_to(REPO))] = hashlib.sha256(path.read_bytes()).hexdigest()
        write_json(output / "report.json", report)
        pipelines = {"baseline": ComparisonPipeline(args.baseline_model, "nano", args.threads),
                     "candidate": CustomPipeline(args.vision_bundle, args.threads)}
        if pipelines["candidate"].detector.bundle.manifest_sha256 != bundle.manifest_sha256:
            raise ValueError("vision bundle changed during comparison startup")
        with (output / "frames.jsonl.partial").open("x", encoding="utf-8") as stream:
            rows = evaluate(images, pipelines, stream)
        report.update(state="complete", source=images.description(), summary_by_split=summarize(rows))
        (output / "frames.jsonl.partial").rename(output / "frames.jsonl")
        write_json(output / "report.json", report)
        print(f"Compared {len(rows)} images: {output / 'report.json'}", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        report.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                      error=f"{type(exc).__name__}: {exc}")
        if output:
            completed = output / "frames.jsonl"
            if completed.exists():
                completed.rename(output / "frames.jsonl.partial")
            write_json(output / "report.json", report)
        print(report["error"], file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
