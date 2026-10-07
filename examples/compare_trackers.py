#!/usr/bin/env python3
"""Compare offline trackers using one frozen detector cache and native images.

No live configuration, service, camera or command transport is opened. The two
replay clocks are controlled schedules, not measurements of flight latency.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO))

from examples.compare_vision import create_output, provenance, write_json
from examples.diagnose_continuity import NativeFlight, digest, distribution, infer, read_windows
from argos.console.config import validate_vision_hz
from argos.perception.appearance import AppearanceEncoder
from argos.perception.continuity_replay import replay
from argos.perception.yolox import YoloXPersonDetector, validate_inference_threads

TRACKERS = ("image", "bytetrack", "botsort")
MODES = ("recorded", "simulated")


def peak_rss():
    # Linux getrusage.ru_maxrss can retain the parent's pre-exec high-water
    # mark after subprocess creation. VmHWM belongs to this executable's mm.
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmHWM:"):
                _, value, unit = line.split()
                if unit != "kB":
                    raise RuntimeError("unsupported Linux VmHWM unit")
                return int(value) * 1024
        raise RuntimeError("Linux process VmHWM unavailable; refusing inherited RSS peak")
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


def semantic_value(value):
    """Remove only execution-cost fields; retain schedules, IDs and decisions."""
    if isinstance(value, dict):
        return {key: semantic_value(item) for key, item in value.items()
                if key not in {"association_cost", "execution_cost"}
                and not key.endswith(("wall_ms", "cpu_ms"))}
    if isinstance(value, list):
        return [semantic_value(item) for item in value]
    return value


def semantic_digest(value):
    return hashlib.sha256(json.dumps(semantic_value(value), sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def load_cache(directory, expected_sha256):
    path = directory / "inference.jsonl"
    if digest(path) != expected_sha256:
        raise ValueError("frozen inference cache changed")
    frames = []
    for line in path.read_text(encoding="utf-8").splitlines():
        frame = json.loads(line)
        sequence = frame.get("sequence")
        if type(sequence) is not int or not 1 <= sequence < 2**53:
            raise ValueError("invalid cached sequence")
        jpeg = (directory / "frames" / f"{sequence}.jpg").read_bytes()
        if hashlib.sha256(jpeg).hexdigest() != frame["sha256"]:
            raise ValueError("cached original JPEG changed")
        frames.append(dict(frame, jpeg=jpeg))
    return frames


def worker(job_path):
    """A fresh subprocess for one tracker/mode/pass, including both windows."""
    job = json.loads(job_path.read_text(encoding="utf-8"))
    started, cpu = time.perf_counter(), time.process_time()
    import cv2
    import numpy as np
    cv2.setNumThreads(job["threads"])
    cv2.setRNGSeed(0)
    np.random.seed(0)
    directory = Path(job["directory"])
    frames = load_cache(directory, job["cache_sha256"])
    baseline = peak_rss()
    from argos.perception.tracker_comparison import make_tracker

    def pixels(frame):
        image = cv2.imdecode(np.frombuffer(frame["jpeg"], dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (frame["height"], frame["width"]):
            raise ValueError("invalid GMC input image")
        return image

    windows = []
    for window in job["windows"]:
        selected = [frame for frame in frames if window["start"] <= frame["received_at"] <= window["end"]]
        wall, cpu_at = time.perf_counter(), time.process_time()
        result = replay(selected, start=window["start"], end=window["end"],
                        selection=window["selection"], mode=job["mode"], max_hz=job["max_hz"],
                        tracker_factory=lambda **kw: make_tracker(job["tracker"], **kw),
                        image_provider=pixels if job["tracker"] == "botsort" else None)
        result["execution_cost"] = {"wall_ms": (time.perf_counter() - wall) * 1000,
                                    "cpu_ms": (time.process_time() - cpu_at) * 1000}
        windows.append({"name": window["name"], **result})
    report = {"tracker": job["tracker"], "mode": job["mode"], "windows": windows,
              "semantic_sha256": semantic_digest(windows),
              "cost": {"process_wall_s": time.perf_counter() - started,
                       "process_cpu_s": time.process_time() - cpu,
                       "baseline_peak_rss_bytes": baseline, "peak_rss_bytes": peak_rss(),
                       "rss_source": "/proc/self/status:VmHWM" if sys.platform.startswith("linux") else "getrusage:ru_maxrss",
                       "cached_jpeg_bytes": sum(len(frame["jpeg"]) for frame in frames),
                       "scope": "fresh offline replay subprocess, includes cache, imports and traces; excludes detector model; not live service RSS"}}
    write_json(Path(job["result"]), report)


def aggregate(passes):
    """Keep independent-process costs separate from common detector timings."""
    grouped = {}
    for tracker in TRACKERS:
        grouped[tracker] = {}
        for mode in MODES:
            runs = [run for run in passes if run["tracker"] == tracker and run["mode"] == mode]
            association = [frame["association"] for run in runs for window in run["windows"]
                           for frame in window["frame_decisions"] if "association" in frame]
            grouped[tracker][mode] = {
                "repeats": len(runs),
                "semantic_repeatable": len({run["semantic_sha256"] for run in runs}) == 1,
                "semantic_sha256": [run["semantic_sha256"] for run in runs],
                "cost": {key: distribution([run["cost"][key] for run in runs]) for key in
                         ("process_wall_s", "process_cpu_s", "peak_rss_bytes", "baseline_peak_rss_bytes")},
                "association_cost": {key: distribution([item[key] for item in association]) for key in
                                     ("tracker_wall_ms", "tracker_cpu_ms", "image_provider_wall_ms", "image_provider_cpu_ms")},
                "windows": [{"name": window["name"], **window["summary"]} for window in runs[0]["windows"]],
            }
    return grouped


def run_pass(output, windows, args, tracker, mode, label, cache_hash):
    stem = f"{label}-{tracker}-{mode}"
    job_path, result_path = output / "passes" / f"{stem}.job.json", output / "passes" / f"{stem}.json"
    job = dict(directory=str(output), windows=windows, threads=args.threads, max_hz=args.max_hz,
               tracker=tracker, mode=mode, cache_sha256=cache_hash, result=str(result_path))
    write_json(job_path, job)
    environment = dict(os.environ, OMP_NUM_THREADS=str(args.threads),
                       OPENBLAS_NUM_THREADS=str(args.threads), MKL_NUM_THREADS=str(args.threads))
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(job_path)],
                               env=environment, text=True, capture_output=True, timeout=300)
    (output / "passes" / f"{stem}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(f"{stem} failed; inspect passes/{stem}.log")
    print(f"Completed {stem}", flush=True)
    return json.loads(result_path.read_text(encoding="utf-8"))


def write_viewer(output, report, representative, frames):
    data = dict(report, runs=representative,
                frames=[{key: frame[key] for key in ("sequence", "received_at", "width", "height", "detections")}
                        for frame in frames])
    encoded = json.dumps(data, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = (REPO / "argos/perception/static/tracker_comparison.html").read_text(encoding="utf-8")
    (output / "index.html").write_text(template.replace("__COMPARISON_DATA__", encoded), encoding="utf-8")


def main(argv=None):
    # Deliberately a data-only internal job; no eval, pickle, devices or services.
    if argv is None and len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flight-dir", required=True, type=Path)
    parser.add_argument("--windows", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--max-hz", type=int, default=10)
    parser.add_argument("--max-frames", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    output = None
    report = {"format": "argos.tracker-comparison", "version": 1, "state": "running"}
    try:
        validate_vision_hz(args.max_hz)
        validate_inference_threads(args.threads)
        if not 2 <= args.repeats <= 10:
            raise ValueError("repeats must be in 2..10")
        if not 1 <= args.max_frames <= 5000:
            raise ValueError("max_frames must be in 1..5000")
        bundle_input = args.bundle.expanduser().resolve()
        bundle_directory = bundle_input if bundle_input.is_dir() else bundle_input.parent
        if args.output_dir.expanduser().resolve().is_relative_to(bundle_directory):
            raise ValueError("keep output outside the model bundle")
        windows = read_windows(args.windows)
        windows_hash = digest(args.windows)
        archive = NativeFlight(args.flight_dir)
        output = create_output(args.output_dir, archive.directory)
        if args.windows.resolve().is_relative_to(output) or args.bundle.resolve().is_relative_to(output):
            raise ValueError("keep window and model inputs outside the output directory")
        (output / "frames").mkdir()
        (output / "passes").mkdir()
        write_json(output / "report.json", report)
        detector = YoloXPersonDetector(bundle_path=args.bundle, threads=args.threads)
        model_hash = detector.model.sha256
        bundle_root = detector.bundle.path.parent
        model_path = bundle_root / detector.model.filename
        if output.is_relative_to(bundle_root):
            raise ValueError("keep output outside the model bundle")
        bundle_hashes = {str(path.relative_to(bundle_root)): digest(path)
                         for path in bundle_root.rglob("*") if path.is_file()}
        encoder = AppearanceEncoder()
        raw_frames = list(archive.read_frames(windows, args.max_frames))
        for _ in range(2):
            infer(raw_frames[0], detector, encoder)
        frames = []
        with (output / "inference.jsonl.partial").open("w", encoding="utf-8") as stream:
            for frame in raw_frames:
                result = infer(frame, detector, encoder)
                result.pop("historical", None)
                frames.append(result)
                stream.write(json.dumps({key: value for key, value in result.items() if key != "jpeg"}, allow_nan=False) + "\n")
                (output / "frames" / f"{frame['sequence']}.jpg").write_bytes(result["jpeg"])
                if len(frames) % 100 == 0:
                    print(f"Frozen {len(frames)}/{len(raw_frames)} original images", flush=True)
        (output / "inference.jsonl.partial").rename(output / "inference.jsonl")
        cache_hash = digest(output / "inference.jsonl")
        code = [Path(__file__), REPO / "examples/diagnose_continuity.py",
                REPO / "argos/perception/continuity_replay.py",
                REPO / "argos/perception/tracker_comparison.py", REPO / "argos/perception/static/tracker_comparison.html",
                REPO / "argos/console/vision.py", REPO / "argos/console/yaw_preview.py",
                REPO / "argos/backends/yaw_stream_source.py", REPO / "argos/backends/vision_bench_source.py"]
        code += list((REPO / "argos/perception/_vendor").rglob("*.py"))
        origin = provenance()
        origin["source_sha256"].update({str(path.relative_to(REPO)): digest(path) for path in code})
        origin["comparison_packages"] = {name: importlib.metadata.version(name) for name in
                                         ("numpy", "opencv-python-headless", "scipy", "lap", "cython_bbox")}
        report.update(source={"directory": str(archive.directory), "sha256": archive.hashes,
                              "declared_drops": archive.drop_counts},
                      model={"path": str(model_path), "sha256": model_hash, "bundle_sha256": bundle_hashes},
                      windows=windows, windows_sha256=windows_hash, provenance=origin,
                      cache={"sha256": cache_hash, "frames": len(frames),
                             "confidence_floor": .35, "nms_threshold": .45, "max_detections": 16},
                      configuration={"threads": args.threads, "max_hz": args.max_hz,
                                     "detector_warmup_images": 2, "detector_measured_passes": 1,
                                     "replay_warmup_processes_per_variant": 1, "replay_measured_repeats": args.repeats,
                                     "replay_process_order": "rotate tracker order per repetition; modes recorded then simulated"},
                      common_worker_cost={"wall_ms": distribution([frame["worker_ms"] for frame in frames]),
                          "stages": {stage: {kind: distribution([frame["timings"][stage][kind] for frame in frames])
                              for kind in ("wall_ms", "cpu_ms")} for stage in ("decode", "detect", "appearance")}})
        write_json(output / "report.json", report)
        for tracker in TRACKERS:
            for mode in MODES:
                run_pass(output, windows, args, tracker, mode, "warmup", cache_hash)
        passes, representative = [], []
        for repetition in range(args.repeats):
            order = TRACKERS[repetition % 3:] + TRACKERS[:repetition % 3]
            for tracker in order:
                for mode in MODES:
                    result = run_pass(output, windows, args, tracker, mode, f"repeat-{repetition + 1}", cache_hash)
                    passes.append(result)
                    if repetition == 0:
                        representative.append(result)
        report["comparison"] = aggregate(passes)
        report["limits"] = [
            "Two short diagnostic windows and sparse reviewed identities are not a dense MOT reference; no IDF1, HOTA or quality winner.",
            "All variants use identical measured detector boxes, scores, heuristic appearances, selection and schedules; no learned ReID.",
            "Native association IDs are mapped to original detections. Omitted boxes remain visible with ephemeral IDs; predictions never count as fresh observations.",
            "Software tracking/dry-valid durations do not measure correct-person tracking between reviewed checkpoints.",
            "Recorded availability is a log-observation proxy. Simulated latest-image scheduling excludes measured tracker/GMC owner blocking, IPC and camera encoding.",
            "GMC image-provider JPEG decoding is an offline harness cost, separately reported; it is not an intrinsic tracker cost.",
            "Common decode/detect/appearance timings are one desktop pass, not a sustained resource test. Repeated isolated replay RSS includes imports, cache and traces, excludes detector weights, and is not live service RSS.",
            "The detector discards scores below .35; native low-score association cannot use .1-.35 evidence. Seven lost-update buffer is not a wall-clock .7-second guarantee.",
            "No service, active model, flight configuration or radio behavior is modified.",
        ]
        archive.verify()
        if (digest(args.windows) != windows_hash or digest(model_path) != model_hash
                or bundle_hashes != {str(path.relative_to(bundle_root)): digest(path)
                                     for path in bundle_root.rglob("*") if path.is_file()}
                or digest(output / "inference.jsonl") != cache_hash
                or any(digest(REPO / path) != sha for path, sha in origin["source_sha256"].items())):
            raise ValueError("source, code, window, model or cache changed during comparison")
        report["state"] = "complete"
        write_viewer(output, report, representative, frames)
        write_json(output / "report.json", report)
        print(f"Comparison saved: {output / 'index.html'}", flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        if output is not None:
            report.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
            write_json(output / "report.json", report)
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
