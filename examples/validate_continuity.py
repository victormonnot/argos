#!/usr/bin/env python3
"""Evaluate a densely reviewed span and annotated recovery counterfactuals offline.

Reuses a completed tracker-comparison cache. No inference, device, transport,
active configuration or production policy is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO))

from examples.compare_trackers import load_cache, semantic_digest, semantic_value
from examples.compare_vision import create_output, write_json
from examples.diagnose_continuity import digest, read_json
from argos.perception.continuity_reference import (
    CONDITIONS, evaluate_replay, make_suppressions, validate_judgments, validate_reference,
)
from argos.perception.continuity_replay import replay
from argos.perception.tracker_comparison import make_tracker


def upstream_projection(run):
    """Everything upstream of the annotated selection input must be unchanged."""
    fields = ("sequence", "received_at", "recorded_observation_at", "worker_ms", "status", "reason",
              "submitted_at", "synthetic_worker_ms", "worker_completed_at", "delivered_at", "image_age_s",
              "detections", "appearance_available", "association")
    return semantic_value({
        "frames": [{key: row[key] for key in fields if key in row} for row in run["frame_decisions"]],
        "events": [event for event in run["events"] if event["kind"] in
                   {"association", "worker_submitted", "result_rejected", "vision_error", "selection"}],
    })


def removed_previous_targets(run):
    """Flag intervention risks; this is not an oracle success criterion."""
    previous_target, rows = None, []
    for decision in run["frame_decisions"]:
        if decision["status"] != "accepted":
            continue
        suppression = decision.get("preview_suppression", {})
        removed = suppression.get("suppressed_detection_indices", [])
        if previous_target is not None:
            matches = [index for index in removed if decision["detections"][index]["track_id"] == previous_target]
            if matches:
                rows.append(dict(sequence=decision["sequence"], previous_selected_track_id=previous_target,
                                 removed_detection_indices=matches,
                                 meaning="selected ID at preceding delivery was removed; intervening state reads may differ"))
        previous_target = decision["preview"]["target_id"]
    return rows


def save_viewer(output, report, frames, reference, judgments):
    image_dir = output / "frames"
    image_dir.mkdir()
    displayed = []
    for frame in frames:
        sequence = frame["sequence"]
        if sequence not in reference:
            continue
        (image_dir / f"{sequence}.jpg").write_bytes(frame["jpeg"])
        displayed.append({**{key: frame[key] for key in ("sequence", "received_at", "width", "height", "detections")},
                          "reference": reference[sequence], "judgments": judgments[sequence]})
    data = dict(report, frames=displayed)
    encoded = json.dumps(data, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = (REPO / "argos/perception/static/continuity_validation.html").read_text(encoding="utf-8")
    (output / "index.html").write_text(template.replace("__VALIDATION_DATA__", encoded), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-comparison", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate-review", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output = None
    report = {"format": "argos.continuity-validation", "version": 1, "state": "running"}
    try:
        source = args.source_comparison.expanduser().resolve()
        base_path = source / "report.json"
        base = read_json(base_path, 16 * 1024 * 1024)
        if (base.get("format") != "argos.tracker-comparison" or base.get("version") != 1
                or base.get("state") != "complete"):
            raise ValueError("a completed tracker comparison is required")
        reference_input = read_json(args.reference, 4 * 1024 * 1024)
        review_input = read_json(args.candidate_review, 4 * 1024 * 1024)
        reference_hash, review_hash, baseline_hash = digest(args.reference), digest(args.candidate_review), digest(base_path)
        windows = [window for window in base["windows"] if window["name"] == reference_input.get("window")]
        if len(windows) != 1:
            raise ValueError("reference must name exactly one source window")
        window = windows[0]
        frames = load_cache(source, base["cache"]["sha256"])
        reference = validate_reference(reference_input, frames, window)
        judgments = validate_judgments(review_input, frames, reference, reference_sha256=reference_hash,
                                       cache_sha256=base["cache"]["sha256"])
        selected = [frame for frame in frames if window["start"] <= frame["received_at"] <= window["end"]]
        masks = {condition: make_suppressions(condition, judgments, selected) for condition in CONDITIONS}
        if any(request["sequence"] not in reference for mask in masks.values() for request in mask):
            raise ValueError("counterfactual outside the reviewed span")
        if reference_input["start"] <= window["selection"]["at"]:
            raise ValueError("review interventions must start after the fixed selection")
        output = create_output(args.output_dir, source)
        write_json(output / "report.json", report)
        code = [Path(__file__), REPO / "argos/perception/continuity_reference.py",
                REPO / "argos/perception/continuity_replay.py", REPO / "argos/perception/tracker_comparison.py",
                REPO / "argos/perception/static/continuity_validation.html"]
        # Retain actual production dependency hashes, allowing only the replay
        # extension and benchmark view/runner to differ from the preceding brick.
        allowed_changes = {"argos/perception/continuity_replay.py",
                           "argos/perception/static/tracker_comparison.html", "examples/compare_trackers.py"}
        for name, sha in base["provenance"]["source_sha256"].items():
            if name not in allowed_changes and digest(REPO / name) != sha:
                raise ValueError(f"source comparison dependency changed: {name}")
        source_hashes = {str(path.relative_to(REPO)): digest(path) for path in code}
        source_hashes.update({name: digest(REPO / name) for name in base["provenance"]["source_sha256"]})
        report.update(source={"comparison": str(source), "report_sha256": baseline_hash,
                              "cache_sha256": base["cache"]["sha256"], "model": base["model"]},
                      reference={"path": str(args.reference.resolve()), "sha256": reference_hash,
                                 "review_status": "assistant_reviewed", "human_validated": False,
                                 "start": reference_input["start"], "end": reference_input["end"],
                                 "source_images": len(reference), "reviewer": reference_input["reviewer"]},
                      candidate_review={"path": str(args.candidate_review.resolve()), "sha256": review_hash},
                      source_sha256=source_hashes, window=window, masks=masks, runs=[],
                      configuration={"trackers": ["image", "bytetrack"], "modes": ["recorded", "simulated"],
                                     "conditions": list(CONDITIONS), "semantic_repetitions": 2,
                                     "max_hz": base["configuration"]["max_hz"]})
        baseline_paths = {}
        for tracker in ("image", "bytetrack"):
            for mode in ("recorded", "simulated"):
                old_path = source / "passes" / f"repeat-1-{tracker}-{mode}.json"
                baseline_paths[old_path] = digest(old_path)
                old_run = next(item for item in read_json(old_path, 32 * 1024 * 1024)["windows"]
                               if item["name"] == window["name"])
                old_run = {key: value for key, value in old_run.items() if key != "name"}
                baseline_upstream = None
                for condition in CONDITIONS:
                    kwargs = dict(start=window["start"], end=window["end"], selection=window["selection"],
                                  mode=mode, max_hz=base["configuration"]["max_hz"],
                                  tracker_factory=lambda **kw: make_tracker(tracker, **kw),
                                  preview_suppressions=None if condition == "unchanged" else masks[condition])
                    run = replay(selected, **kwargs)
                    repeated = replay(selected, **kwargs)
                    if semantic_digest(run) != semantic_digest(repeated):
                        raise ValueError("counterfactual outcomes are not repeatable")
                    current_upstream = upstream_projection(run)
                    if condition == "unchanged":
                        if semantic_digest(run) != semantic_digest(old_run):
                            raise ValueError("default replay no longer matches the source comparison")
                        baseline_upstream = current_upstream
                    elif current_upstream != baseline_upstream:
                        raise ValueError("annotated suppression changed association, initial selection or scheduling")
                    evaluation = evaluate_replay(run, reference, judgments,
                        start=reference_input["start"], end=reference_input["end"])
                    report["runs"].append(dict(tracker=tracker, mode=mode, condition=condition, run=run,
                        evaluation=evaluation, semantic_sha256=semantic_digest(run), semantic_repeatable=True,
                        upstream_unchanged=True, removed_previously_selected_ids=removed_previous_targets(run)))
                    print(f"Validated {tracker}/{mode}/{condition}", flush=True)
        report["limits"] = [
            "Dense assistant review of one known diagnostic span, not human ground truth or independent validation; prior outcomes were known.",
            "Exact admitted-image denominators only. Unknown identity/extent and skipped images remain explicit; no nearest-frame substitutions or dense IDF1/HOTA claims.",
            "Candidate class agreement and overlap with approximate visible extent are separate; a partial target box can still depict the target.",
            "Annotation-assisted removal occurs after unchanged association. The fixed highest-score survivor can remove the previously selected ID; benefit is not guaranteed.",
            "Unknown candidates and other people always remain. Perfect reviewed grouping is unavailable online; this is not a deployable deduplication policy.",
            "Avoiding ambiguity immediately can still end in a recovery deadline, a later stop, or an unresolved pause when the window ends.",
            "No tracker, detector or live policy change; B2 desktop CPU/RSS remains the applicable bounded measurement. Oracle execution is not a live resource benchmark.",
            "Simulated scheduling excludes measured owner blocking/IPC; phase durations and dry admission are software states, not physical tracking performance.",
        ]
        if (digest(base_path) != baseline_hash or digest(args.reference) != reference_hash
                or digest(args.candidate_review) != review_hash
                or digest(source / "inference.jsonl") != base["cache"]["sha256"]
                or any(digest(path) != sha for path, sha in baseline_paths.items())
                or any(digest(REPO / name) != sha for name, sha in source_hashes.items())):
            raise ValueError("frozen evidence or implementation changed during validation")
        # Recheck all original JPEG files, not only the displayed span.
        for frame in frames:
            if digest(source / "frames" / f"{frame['sequence']}.jpg") != frame["sha256"]:
                raise ValueError("source image changed during validation")
        report["state"] = "complete"
        save_viewer(output, report, frames, reference, judgments)
        write_json(output / "report.json", report)
        print(f"Validation saved: {output / 'index.html'}", flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        if output is not None:
            report.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
            write_json(output / "report.json", report)
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
