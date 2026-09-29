#!/usr/bin/env python3
"""Compare Tiny CPU thread limits offline; no camera, console or radio access.

Uses the installed ARGOS detector and its verified local weights. A repeatable
synthetic JPEG exercises the fixed-size network, not recognition accuracy or
the live pipeline. Stop other inference jobs before measuring.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


THREADS = (1, 2, 4)


def run_worker(model, threads, samples):
    import cv2
    import numpy as np
    from argos.perception.yolox import YoloXPersonDetector

    detector = YoloXPersonDetector(model, variant="tiny", threads=threads)
    # Every child sees identical input. No personal image or external resource.
    image = np.random.default_rng(20260929).integers(
        0, 256, size=(480, 640, 3), dtype=np.uint8)
    encoded, jpeg = cv2.imencode(".jpg", image)
    if not encoded:
        raise ValueError("Could not encode the benchmark image")
    jpeg = jpeg.tobytes()
    # Exclude model loading, graph initialization and the first three forwards.
    for _ in range(3):
        detector.detect(jpeg)
    rows = []
    for _ in range(samples):
        started = time.perf_counter()
        result = detector.detect(jpeg)
        rows.append({"detector_ms": (time.perf_counter() - started) * 1000,
                     "inference_ms": result["inference_ms"]})
    return {"threads": threads, "opencv": cv2.__version__, "samples": rows}


def machine_info():
    cpu = platform.processor() or platform.machine()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.partition(":")[2].strip()
                break
    except OSError:
        pass
    allowed = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    return {"cpu": cpu, "logical_cpus_available": allowed,
            "python": platform.python_version(), "platform": platform.platform()}


def summary(rows):
    result = {}
    for key in ("inference_ms", "detector_ms"):
        values = sorted(row[key] for row in rows)
        if not values or not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("Worker returned invalid timings")
        result[key] = {"median": round(statistics.median(values), 2),
                       "p90": round(values[math.ceil(.9 * len(values)) - 1], 2)}
    result["samples"] = len(rows)
    return result


def benchmark(model, samples, rounds):
    print("Offline YOLOX-Tiny thread comparison. Stop ARGOS inference first.", flush=True)
    print("Synthetic image; no camera, USB, network or configuration changes.", flush=True)
    report = {"machine": machine_info(), "input": "synthetic JPEG 640x480",
              "results": {}}
    print(json.dumps(report["machine"]), flush=True)
    rows = {threads: [] for threads in THREADS}
    for number in range(rounds):
        # Rotate order to reduce systematic first/last bias. Independent child
        # processes isolate OpenCV's process-wide thread setting and graph.
        offset = number % len(THREADS)
        for threads in THREADS[offset:] + THREADS[:offset]:
            print(f"Round {number + 1}/{rounds}, {threads} thread(s)...", flush=True)
            command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                       str(threads), "--model", str(model), "--samples", str(samples)]
            child = subprocess.run(command, capture_output=True, text=True, timeout=120)
            if child.returncode:
                raise ValueError(f"Worker failed: {child.stderr.strip()[-1200:]}")
            data = json.loads(child.stdout)
            if data["threads"] != threads or len(data["samples"]) != samples:
                raise ValueError("Worker returned an inconsistent sample batch")
            summary(data["samples"])
            rows[threads].extend(data["samples"])
            report["opencv"] = data["opencv"]
    print("\nThreads | network median / p90 | detector median / p90 (ms)", flush=True)
    for threads in THREADS:
        result = summary(rows[threads])
        report["results"][str(threads)] = result
        network, detector = result["inference_ms"], result["detector_ms"]
        print(f"{threads:7} | {network['median']:7.2f} / {network['p90']:7.2f}"
              f" | {detector['median']:7.2f} / {detector['p90']:7.2f}", flush=True)
    print("RESULT " + json.dumps(report), flush=True)
    print("Finished. No setting changed. Live camera/appearance/tracking, scheduling,"
          " HTTP and radio timing are NOT measured; validate a candidate live next.")


def main(argv=None):
    from argos.perception.yolox import default_model_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=default_model_path("tiny"))
    parser.add_argument("--samples", type=int, choices=range(2, 31), default=10,
                        metavar="2..30", help="measured forwards per batch (default: 10)")
    parser.add_argument("--rounds", type=int, choices=range(1, 4), default=3,
                        help="rotating-order passes (default: 3)")
    parser.add_argument("--worker", type=int, choices=THREADS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker is not None:
            print(json.dumps(run_worker(args.model, args.worker, args.samples)))
        else:
            benchmark(args.model.expanduser().resolve(), args.samples, args.rounds)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Benchmark stopped: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Benchmark interrupted; no setting changed.\n")


if __name__ == "__main__":
    main()
