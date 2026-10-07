#!/usr/bin/env python3
"""Diagnose selected-person continuity from a completed native .flight archive.

No camera, server, radio or command transport is opened. Recorded result times
are log-observation proxies; desktop scheduling is a separate controlled model.
Neither replay supplies human identity truth or reproduces aircraft motion.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import resource
import statistics
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO))

from examples.compare_vision import create_output, provenance, write_json
from argos.console.config import validate_vision_hz
from argos.console.vision import VisionService
from argos.perception.appearance import AppearanceEncoder
from argos.perception.continuity_replay import replay
from argos.perception.yolox import YoloXPersonDetector, default_model_path, validate_inference_threads


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def count(value):
    return type(value) is int and value >= 0


def read_json(path, limit=1024 * 1024):
    if path.stat().st_size > limit:
        raise ValueError(f"JSON exceeds its size limit: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path, *, max_bytes=128 * 1024 * 1024, max_rows=200000):
    if path.stat().st_size > max_bytes:
        raise ValueError(f"JSONL exceeds its size limit: {path.name}")
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index >= max_rows or len(line) > 1024 * 1024:
                raise ValueError("JSONL exceeds its row limit")
            if line.strip():
                yield json.loads(line)


def read_windows(path):
    value = read_json(path)
    windows = value.get("windows") if isinstance(value, dict) else None
    if not isinstance(windows, list) or not 1 <= len(windows) <= 8:
        raise ValueError("windows must contain 1..8 explicit windows")
    names = set()
    for window in windows:
        if not isinstance(window, dict):
            raise ValueError("each window must be an object")
        name = window.get("name")
        start, end, selection = (window.get(k) for k in ("start", "end", "selection"))
        if (not isinstance(name, str) or not name or len(name) > 100 or name in names
                or not number(start) or not number(end) or not 0 < end - start <= 120
                or not isinstance(selection, dict) or not number(selection.get("at"))
                or not start <= selection["at"] < end):
            raise ValueError("window needs a unique name, <=120 seconds and one selection inside it")
        box = selection.get("box")
        if (not isinstance(box, list) or len(box) != 4 or not all(number(v) for v in box)
                or min(box[2:]) <= 0 or box[0] + box[2] > 1 or box[1] + box[3] > 1
                or not number(selection.get("min_iou", .5))
                or not 0 < selection.get("min_iou", .5) <= 1):
            raise ValueError("selection needs normalized xywh and min_iou in (0,1]")
        names.add(name)
    return windows


class NativeFlight:
    """Read byte-exact native JPEG slices; refuse incomplete or unbound inputs."""

    NAMES = ("manifest.json", "camera.json", "camera.frames.jsonl", "camera.mjpeg", "events.jsonl")

    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.paths = {name: self.directory / name for name in self.NAMES}
        for path in self.paths.values():
            if not path.is_file() or path.resolve().parent != self.directory:
                raise ValueError("native inputs must be files inside the archive directory")
        self.manifest = read_json(self.paths["manifest.json"])
        self.camera = read_json(self.paths["camera.json"])
        if (not isinstance(self.manifest, dict) or not isinstance(self.camera, dict)
                or self.manifest.get("format") != "argos.filming"
                or type(self.manifest.get("schema_version")) is not int
                or self.manifest["schema_version"] != 1
                or self.manifest.get("state") != "complete"
                or self.manifest.get("writer_stopped") is not True
                or self.manifest.get("error")
                or self.manifest.get("failed_events") != 0
                or self.manifest.get("pending_events") != 0
                or self.camera.get("state") != "complete"
                or self.camera.get("writer_stopped") is not True
                or self.camera.get("error") or self.camera.get("discarded_error") != 0):
            raise ValueError("a completed argos.filming v1 archive is required")
        if (any(not isinstance(self.manifest.get(k), str) or not self.manifest[k]
                for k in ("id", "run_id"))
                or self.camera.get("session_id") != self.manifest["id"]
                or type(self.camera.get("schema")) is not int or self.camera["schema"] != 1
                or self.camera.get("format") != "mjpeg_with_receive_timestamp_index"
                or self.camera.get("media") != "camera.mjpeg"
                or self.camera.get("index") != "camera.frames.jsonl"):
            raise ValueError("native camera manifest is not bound to this capture")
        self.origin = self.manifest.get("started_at")
        self.ended = self.manifest.get("ended_at")
        if not number(self.origin) or not number(self.ended) or self.ended <= self.origin:
            raise ValueError("invalid native capture timeline")
        if (not number(self.camera.get("started_at")) or not number(self.camera.get("ended_at"))
                or abs(self.camera["started_at"] - self.origin) > 1e-8
                or not self.origin <= self.camera["ended_at"] <= self.ended):
            raise ValueError("native camera timeline differs from its capture")
        camera_counts = ("written_frames", "accepted_frames", "size_bytes", "index_bytes",
                         "buffered_frames", "buffered_bytes", "dropped_frames", "dropped_queue",
                         "dropped_contention", "dropped_invalid", "dropped_limit", "discarded_error")
        if (any(not count(self.camera.get(k)) for k in camera_counts)
                or any(not count(self.manifest.get(k)) for k in
                       ("events", "bytes", "dropped_events", "failed_events", "pending_events"))
                or self.camera["accepted_frames"] != self.camera["written_frames"]
                or self.camera["buffered_frames"] != 0 or self.camera["buffered_bytes"] != 0
                or self.camera["dropped_frames"] != sum(self.camera[k] for k in
                    ("dropped_queue", "dropped_contention", "dropped_invalid", "dropped_limit"))):
            raise ValueError("native writer counts are inconsistent")
        # Finalized captures may contain declared drops. Preserve their actual
        # gaps and expose those counts; never treat complete=False as corruption.
        self.drop_counts = {"camera_frames": self.camera["dropped_frames"],
                            "events": self.manifest["dropped_events"]}
        self.hashes = {name: digest(path) for name, path in self.paths.items()}
        self.frames, self.events = [], {}
        previous_time, previous_sequence, previous_end = -1., -1, 0
        context = None
        media_size = self.paths["camera.mjpeg"].stat().st_size
        if (media_size != self.camera["size_bytes"]
                or self.paths["camera.frames.jsonl"].stat().st_size != self.camera["index_bytes"]
                or self.paths["events.jsonl"].stat().st_size != self.manifest["bytes"]):
            raise ValueError("native file sizes differ from their finalized manifests")
        for row in lines(self.paths["camera.frames.jsonl"], max_bytes=32 * 1024 * 1024):
            if not isinstance(row, dict):
                raise ValueError("native camera index rows must be objects")
            seq, at, offset, size = (row.get(k) for k in ("sequence", "received_at", "offset", "size_bytes"))
            if (type(row.get("schema")) is not int or row["schema"] != 1
                    or row.get("session_id") != self.manifest["id"]
                    or type(row.get("frame")) is not int or row["frame"] != len(self.frames)
                    or row.get("source") != "device" or not isinstance(row.get("source_id"), str)
                    or not row["source_id"] or type(seq) is not int or seq <= max(previous_sequence, 0)
                    or not number(at) or at <= previous_time or not self.origin <= at <= self.camera["ended_at"]
                    or not number(row.get("elapsed_s")) or abs(row["elapsed_s"] - (at - self.origin)) > 1e-8
                    or type(offset) is not int or offset != previous_end
                    or type(size) is not int or not 1 <= size <= 16 * 1024 * 1024
                    or offset + size > media_size
                    or any(type(row.get(k)) is not int or not 1 <= row[k] <= 4096
                           for k in ("width", "height"))):
                raise ValueError("invalid chronological native camera index")
            current = row["source_id"], row["width"], row["height"]
            if context is not None and current != context:
                raise ValueError("split source/dimension changes into separate diagnostic captures")
            context = current
            self.frames.append(dict(row, received_at=at - self.origin))
            previous_time, previous_sequence, previous_end = at, seq, offset + size
        if not self.frames:
            raise ValueError("native camera archive is empty")
        if (len(self.frames) != self.camera["written_frames"] or previous_end != media_size
                or not number(self.camera.get("first_received_at"))
                or not number(self.camera.get("last_received_at"))
                or abs(self.camera["first_received_at"] - self.origin - self.frames[0]["received_at"]) > 1e-8
                or abs(self.camera["last_received_at"] - previous_time) > 1e-8):
            raise ValueError("native camera index is incomplete or inconsistent with its manifest")
        by_sequence = {row["sequence"]: row for row in self.frames}
        previous_observation, previous_receipt, previous_vision_sequence = -1., -1., 0
        self.unmatched_events = 0
        event_count = 0
        for event in lines(self.paths["events.jsonl"]):
            event_count += 1
            if not isinstance(event, dict):
                raise ValueError("native event rows must be objects")
            if event.get("kind") != "vision":
                continue
            at, seq = event.get("at"), event.get("frame_sequence")
            receipt = event.get("image_received_at")
            if (event.get("run_id") != self.manifest["run_id"]
                    or event.get("recording_id") != self.manifest["id"]
                    or type(event.get("schema_version")) is not int or event["schema_version"] != 1
                    or not number(at) or not self.origin <= at <= self.ended or at < previous_observation
                    or type(seq) is not int or seq <= previous_vision_sequence
                    or not number(receipt) or not self.origin <= receipt <= at or receipt <= previous_receipt):
                raise ValueError("invalid native vision event timeline/binding")
            previous_observation, previous_receipt, previous_vision_sequence = at, receipt, seq
            result = event.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("detections"), list):
                raise ValueError("invalid native vision result")
            # Saved results also contain track_id; validate detector measurements
            # through the production contract without treating those IDs as truth.
            if any(not isinstance(d, dict) or "box" not in d or "confidence" not in d
                   for d in result["detections"]):
                raise ValueError("invalid native historical detections")
            VisionService._validate_result({**result, "detections": [
                {"box": d["box"], "confidence": d["confidence"]} for d in result["detections"]]})
            row = by_sequence.get(seq)
            if row is None:
                self.unmatched_events += 1
                continue
            if (event.get("video_id") != row["source_id"] or not number(receipt)
                    or abs(receipt - self.origin - row["received_at"]) > 1e-8 or at < receipt
                    or (result["width"], result["height"]) != (row["width"], row["height"])
                    or seq in self.events):
                raise ValueError("native vision event does not match its original camera image")
            self.events[seq] = dict(event, at=at - self.origin)
        if event_count != self.manifest["events"]:
            raise ValueError("native event count differs from its finalized manifest")

    def read_frames(self, windows, max_frames):
        if type(max_frames) is not int or not 1 <= max_frames <= 5000:
            raise ValueError("max_frames must be in 1..5000")
        for w in windows:
            if w["start"] < self.frames[0]["received_at"] or w["end"] > self.ended - self.origin:
                raise ValueError("diagnostic windows must be covered by the native capture")
        rows = [row for row in self.frames if any(w["start"] <= row["received_at"] <= w["end"] for w in windows)]
        if not rows or len(rows) > max_frames:
            raise ValueError("window frame count is empty or exceeds max_frames; never silently truncate")
        with self.paths["camera.mjpeg"].open("rb") as media:
            for row in rows:
                media.seek(row["offset"])
                data = media.read(row["size_bytes"])
                if len(data) != row["size_bytes"] or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                    raise ValueError("invalid original JPEG slice")
                event = self.events.get(row["sequence"])
                yield {**row, "jpeg": data, "sha256": hashlib.sha256(data).hexdigest(),
                       "available_at": event["at"] if event else None,
                       "historical": event}

    def verify(self):
        if self.hashes != {name: digest(path) for name, path in self.paths.items()}:
            raise ValueError("native source changed during diagnosis")


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "median": None, "p95": None, "max": None}
    index = .95 * (len(values) - 1)
    lower = math.floor(index)
    p95 = values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)
    return dict(count=len(values), median=statistics.median(values), p95=p95, max=max(values))


def infer(frame, detector, encoder):
    timings = {}
    def measure(name, operation):
        wall, cpu = time.perf_counter(), time.process_time()
        result = operation()
        timings[name] = {"wall_ms": (time.perf_counter() - wall) * 1000,
                         "cpu_ms": (time.process_time() - cpu) * 1000}
        return result
    image = measure("decode", lambda: detector.decode_jpeg(frame["jpeg"]))
    detected = measure("detect", lambda: detector.detect_bgr(image))
    if (detected["width"], detected["height"]) != (frame["width"], frame["height"]):
        raise ValueError("decoded original dimensions differ from native index")
    appearances = measure("appearance", lambda: encoder.encode_bgr(
        image, detected["detections"], width=detected["width"], height=detected["height"]))
    return {**frame, **detected, "appearances": appearances, "timings": timings,
            "worker_ms": sum(t["wall_ms"] for t in timings.values())}


def detection_parity(frames):
    """Numerical comparison to saved boxes, never comparison to human truth."""
    count = unequal = 0
    largest = 0.
    for frame in frames:
        if frame.get("historical") is None:
            continue
        count += 1
        historic = frame["historical"]["result"]["detections"]
        current = frame["detections"]
        if len(historic) != len(current):
            unequal += 1
            continue
        delta = max([abs(a - b) for old, new in zip(historic, current)
                     for a, b in zip([*old["box"], old["confidence"]], [*new["box"], new["confidence"]])] or [0.])
        largest = max(largest, delta)
        unequal += delta > 1e-5
    return {"compared_frames": count, "different_frames_at_1e_5": unequal,
            "max_coordinate_or_score_delta_equal_count_frames": largest,
            "meaning": "saved detector-output agreement, not detection accuracy or historic identity parity"}


def save_viewer(output, frames, report):
    """Self-contained directory with original JPEGs; no network dependencies."""
    images = output / "frames"
    images.mkdir()
    for frame in frames:
        (images / f"{frame['sequence']}.jpg").write_bytes(frame["jpeg"])
    data = dict(report, frames=[{k: v for k, v in f.items() if k not in {"jpeg", "appearances"}} for f in frames])
    encoded = json.dumps(data, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = (REPO / "argos/perception/static/continuity_replay.html").read_text(encoding="utf-8")
    (output / "index.html").write_text(template.replace("__REPLAY_DATA__", encoded), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flight-dir", type=Path, required=True)
    parser.add_argument("--windows", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, default=default_model_path("nano"))
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--max-frames", type=int, default=1000)
    parser.add_argument("--max-hz", type=int, default=10)
    parser.add_argument("--extra-worker-ms", type=float, default=0., help="declared synthetic delay, simulated mode only")
    args = parser.parse_args(argv)
    output = None
    report = {"format": "argos.continuity-diagnostic", "version": 1, "state": "running"}
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    try:
        validate_vision_hz(args.max_hz)
        validate_inference_threads(args.threads)
        if not number(args.extra_worker_ms):
            raise ValueError("extra_worker_ms must be finite and nonnegative")
        windows = read_windows(args.windows)
        windows_hash = digest(args.windows)
        archive = NativeFlight(args.flight_dir)
        output = create_output(args.output_dir, archive.directory)
        if args.windows.resolve().is_relative_to(output):
            raise ValueError("keep windows outside the output directory")
        write_json(output / "report.json", report)
        detector = YoloXPersonDetector(args.model_path, variant="nano", threads=args.threads)
        model_hash = detector.model.sha256
        encoder = AppearanceEncoder()
        raw_frames = list(archive.read_frames(windows, args.max_frames))
        for _ in range(2):
            infer(raw_frames[0], detector, encoder)
        frames = []
        with (output / "inference.jsonl.partial").open("w", encoding="utf-8") as stream:
            for frame in raw_frames:
                result = infer(frame, detector, encoder)
                frames.append(result)
                stream.write(json.dumps({k: v for k, v in result.items() if k != "jpeg"}, allow_nan=False) + "\n")
                if len(frames) % 50 == 0:
                    print(f"Inferred {len(frames)}/{len(raw_frames)} original images", flush=True)
        source_code = [Path(__file__), REPO / "argos/perception/continuity_replay.py",
                       REPO / "argos/console/vision.py", REPO / "argos/console/yaw_preview.py",
                       REPO / "argos/backends/yaw_stream_source.py", REPO / "argos/backends/vision_bench_source.py"]
        origin = provenance()
        origin["source_sha256"].update({str(p.relative_to(REPO)): digest(p) for p in source_code})
        report.update(source={"directory": str(archive.directory), "sha256": archive.hashes,
                              "historical_manifest": archive.manifest,
                              "declared_drops": archive.drop_counts,
                              "unmatched_historical_vision_events": archive.unmatched_events},
                      windows_source={"path": str(args.windows.resolve()), "sha256": windows_hash},
                      provenance=origin, model={"variant": "nano", "path": str(args.model_path.resolve()),
                                               "sha256": model_hash},
                      configuration={"threads": args.threads, "max_hz": args.max_hz, "tick_seconds": .01,
                                     "extra_worker_ms": args.extra_worker_ms, "warmups": 2, "measured_passes": 1},
                      detection_parity=detection_parity(frames), windows=[])
        for window in windows:
            selected = [f for f in frames if window["start"] <= f["received_at"] <= window["end"]]
            runs = {}
            for mode in ("recorded", "simulated"):
                wall, cpu = time.perf_counter(), time.process_time()
                runs[mode] = replay(selected, start=window["start"], end=window["end"],
                    selection=window["selection"], mode=mode, max_hz=args.max_hz,
                    extra_worker_ms=args.extra_worker_ms if mode == "simulated" else 0.)
                runs[mode]["execution_cost"] = {"wall_ms": (time.perf_counter() - wall) * 1000,
                                                "cpu_ms": (time.process_time() - cpu) * 1000}
            report["windows"].append({**window, "source_frames": len(selected), "runs": runs})
        report["cost"] = {"inference_stages": {stage: {kind: distribution([f["timings"][stage][kind] for f in frames])
                            for kind in ("wall_ms", "cpu_ms")} for stage in ("decode", "detect", "appearance")},
                          "worker_wall_ms": distribution([f["worker_ms"] for f in frames]),
                          "harness_cpu_s": time.process_time() - cpu_start,
                          "harness_wall_s": time.perf_counter() - wall_start,
                          "harness_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                          "cached_jpeg_bytes": sum(len(f["jpeg"]) for f in frames),
                          "scope": "single desktop CPU pass; RSS includes model, cached images and diagnostic traces; not live worker RSS"}
        report["limits"] = [
            "No human-validated temporal identity reference: no target accuracy, IDF1, HOTA or quality winner.",
            "Selections are explicit controlled initializations by box, not reconstructed operator intent or historical tracker state.",
            "Recorded mode uses logged result observation times, an upper-bound proxy for availability; rejected workers and intervening live polls are unknown.",
            "Simulated mode uses production VisionService with cached measured worker times; excludes IPC, camera encoding and owner contention.",
            "Dry consumer uses the production YawValidator without radio/pilot/flight-controller authority; valid means only software image admission.",
            "Image times are host receipt, not camera exposure. Historical runtime may differ from current source.",
            "Two warmups and one inference pass are diagnostic timing, not a sustained resource benchmark.",
        ]
        archive.verify()
        if digest(args.model_path) != model_hash:
            raise ValueError("model file changed during diagnosis")
        if digest(args.windows) != windows_hash:
            raise ValueError("windows changed during diagnosis")
        (output / "inference.jsonl.partial").rename(output / "inference.jsonl")
        report["state"] = "complete"
        save_viewer(output, frames, report)
        write_json(output / "report.json", report)
        print(f"Diagnostic saved: {output / 'index.html'}", flush=True)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, KeyboardInterrupt) as exc:
        if output is not None:
            report.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
            write_json(output / "report.json", report)
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
