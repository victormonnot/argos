#!/usr/bin/env python3
"""Compare bounded offline recovery policies with unchanged detector/association.

No device, radio, inference, service or active configuration is opened. Reviewed
labels reach the evaluator only, never a policy or replay worker.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO))

from examples.compare_trackers import load_cache, peak_rss, semantic_digest
from examples.compare_vision import create_output, write_json
from examples.diagnose_continuity import digest, distribution, read_json
from examples.validate_continuity import upstream_projection
from argos.perception.continuity_reference import evaluate_replay, validate_judgments, validate_reference
from argos.perception.continuity_replay import replay

POLICIES = ("current", "motion", "motion_duplicates")
HELD_POLICIES = (*POLICIES, "motion_held", "motion_held_duplicates")
CANDIDATE_POLICIES = (*HELD_POLICIES, "motion_held_candidates")
MODES = ("recorded", "simulated")

# Frozen before multi-person outcomes. This replaces only the lost/paused
# multi-strong stop; ordinary single-candidate recovery retains its B5 contract.
CANDIDATE_CONTRACT = dict(
    scope="lost_or_paused_multi_strong_episode", strong_confidence_min=.5,
    persist_after_competitor_disappears=True, clear_pending_on_episode_entry=True,
    appearance_similarity_min=.88, competitor_similarity_margin=.05,
    margin_tolerance=1e-12, multiple_eligible="pause",
    plausible_unknown_competitor="pause", no_eligible="pause",
    geometry="unchanged_held_dx_vertical_and_scale_gates",
    confirmation="same_candidate_two_distinct_sequences_and_receipts_in_episode",
    refusal_clears_pending=True, raw_detections_preserved=True,
    unchanged_outside_episode=True, unchanged_deadline_and_consumer=True,
    annotations_available_to_policy=False, learned_reid=False,
    identity_limits=["stable_native_id_reuse", "observationally_indistinguishable_people"])


def worker(job_path):
    """One fresh subprocess, both original windows, and no reference labels."""
    job = read_json(job_path, 1024 * 1024)
    if job["policy"] not in CANDIDATE_POLICIES or job["mode"] not in MODES:
        raise ValueError("unknown recovery policy or clock")
    started, cpu = time.perf_counter(), time.process_time()
    import cv2
    import numpy as np
    from argos.perception.tracker_comparison import make_tracker
    from argos.perception.recovery_prototype import make_preview
    cv2.setNumThreads(job["threads"])
    cv2.setRNGSeed(0)
    np.random.seed(0)
    frames = load_cache(Path(job["source"]), job["cache_sha256"])
    baseline_rss = peak_rss()
    results = []
    for window in job["windows"]:
        selected = [frame for frame in frames if window["start"] <= frame["received_at"] <= window["end"]]
        wall, cpu_at = time.perf_counter(), time.process_time()
        factory = (None if job["policy"] == "current" else
                   lambda **kwargs: make_preview(job["policy"], **kwargs))
        result = replay(selected, start=window["start"], end=window["end"],
                        selection=window["selection"], mode=job["mode"], max_hz=job["max_hz"],
                        tracker_factory=lambda **kwargs: make_tracker("bytetrack", **kwargs),
                        preview_factory=factory, measure_preview=True)
        result["execution_cost"] = dict(wall_ms=(time.perf_counter() - wall) * 1000,
                                        cpu_ms=(time.process_time() - cpu_at) * 1000)
        results.append(dict(name=window["name"], **result))
    write_json(Path(job["result"]), dict(policy=job["policy"], mode=job["mode"], windows=results,
        cost=dict(process_wall_s=time.perf_counter() - started, process_cpu_s=time.process_time() - cpu,
                  baseline_peak_rss_bytes=baseline_rss, peak_rss_bytes=peak_rss(),
                  rss_source="/proc/self/status:VmHWM" if sys.platform.startswith("linux") else "getrusage:ru_maxrss",
                  scope="fresh replay process including cache/imports/traces; excludes detector and service; not live RSS")))


def verify_protocol(protocol):
    """Refuse a frozen protocol describing a different experiment."""
    expected_motion = dict(horizontal_only=True, history_s=.7, min_selected_distinct_measurements=2,
                           horizon_s=.5, residual_max_dx=.25, predicted_commands=False,
                           velocity="last two selected measurements; clamp +/-1 normalized width/s",
                           fallback="unchanged static geometry when insufficient/old history",
                           appearance_vertical_scale_gates="unchanged production reference and thresholds")
    expected_duplicates = dict(iou_min=.85, appearance_similarity_min=.95,
                               all_strong_pairwise_clique=True, annotations_available_to_policy=False,
                               priority=["selected ID", "pending recovery ID", "confidence", "source index"],
                               missing_appearance="no grouping")
    version = protocol.get("version")
    policies = CANDIDATE_POLICIES if version == 3 else HELD_POLICIES if version == 2 else POLICIES
    if (protocol.get("format") != "argos.recovery-prototype.protocol" or type(protocol.get("version")) is not int
            or version not in (1, 2, 3)
            or protocol.get("policies") != list(policies) or protocol.get("clocks") != list(MODES)
            or protocol.get("trackers") != ["bytetrack"]
            or protocol.get("evaluation", {}).get("repeats") != 3
            or any(protocol.get("motion", {}).get(k) != v for k, v in expected_motion.items())
            or any(protocol.get("duplicates", {}).get(k) != v for k, v in expected_duplicates.items())):
        raise ValueError("protocol does not describe the fixed recovery experiment")
    if version in (2, 3):
        expected_held = dict(estimate_history_reference="anchor_receipt", sample_span_s=.7,
            horizon_s=.5, velocity_max_widths_per_s=1., uncertainty_rate_widths_per_s=.1,
            uncertainty_kind="fixed_assumption_not_calibrated", gate="absolute_residual_plus_margin",
            renewal="selected_strong_measurement_advancing_sequence_and_receipt_only",
            fallback="unchanged_static_geometry_when_estimate_unavailable_or_expired",
            predicted_commands=False, legacy_policies_unchanged=True)
        if protocol.get("held_motion") != expected_held:
            raise ValueError("protocol does not describe the fixed held-motion experiment")
    if version == 3 and protocol.get("candidate_recovery") != CANDIDATE_CONTRACT:
        raise ValueError("protocol does not describe the fixed candidate-qualification experiment")
    bindings = protocol.get("input_sha256")
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError("protocol must bind its input evidence")
    for path, sha in bindings.items():
        if digest(Path(path)) != sha:
            raise ValueError(f"frozen protocol input changed: {path}")
    return policies


def collect_runs(passes, originals, reference, judgments, reference_input, *,
                 policies=POLICIES, previous=None, previous_policies=POLICIES):
    """Verify repeats/upstream/defaults before computing any reviewed metrics."""
    rows, costs = [], []
    for policy in policies:
        for mode in MODES:
            group = [item for item in passes if item["policy"] == policy and item["mode"] == mode]
            if len(group) != 3:
                raise ValueError("three independent measured repeats are required")
            first = group[0]
            if any(semantic_digest(item["windows"]) != semantic_digest(first["windows"]) for item in group[1:]):
                raise ValueError("recovery policy outcomes are not repeatable")
            for result in first["windows"]:
                name = result["name"]
                run = {k: v for k, v in result.items() if k != "name"}
                old = originals[mode, name]
                if upstream_projection(run) != upstream_projection(old):
                    raise ValueError("recovery policy changed association, selection or scheduling")
                if policy == "current" and semantic_digest(run) != semantic_digest(old):
                    raise ValueError("default recovery no longer reproduces the source comparison")
                if previous is not None and policy in previous_policies:
                    if semantic_digest(run) != semantic_digest(previous[policy, mode, name]):
                        raise ValueError("legacy recovery policy no longer reproduces the previous comparison")
                evaluation = (evaluate_replay(run, reference, judgments, start=reference_input["start"],
                                             end=reference_input["end"])
                              if name == reference_input["window"] else None)
                rows.append(dict(policy=policy, mode=mode, window=name, run=run, evaluation=evaluation,
                                 semantic_repeatable=True, upstream_unchanged=True,
                                 previous_semantic_parity=(True if previous is not None and policy in previous_policies else None),
                                 semantic_sha256=semantic_digest(run)))
            costs.append(dict(policy=policy, mode=mode,
                process={key: distribution([item["cost"][key] for item in group])
                         for key in ("process_wall_s", "process_cpu_s", "peak_rss_bytes")},
                preview=[dict(window=window["name"], repeats=[
                    next(w for w in item["windows"] if w["name"] == window["name"])["summary"]["execution_cost"]
                    for item in group]) for window in first["windows"]],
                scope=first["cost"]["scope"]))
    return rows, costs


def save_viewer(output, report, frames, reference, judgments):
    (output / "frames").mkdir()
    displayed = []
    for frame in frames:
        windows = [w for w in report["windows"] if w["start"] <= frame["received_at"] <= w["end"]]
        if not windows:
            continue
        sequence = frame["sequence"]
        (output / "frames" / f"{sequence}.jpg").write_bytes(frame["jpeg"])
        item = {key: frame[key] for key in ("sequence", "received_at", "width", "height", "detections")}
        item["window"] = windows[0]["name"]
        if sequence in reference:
            item.update(reference=reference[sequence], judgments=judgments[sequence])
        displayed.append(item)
    encoded = json.dumps(dict(report, frames=displayed), allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = (REPO / "argos/perception/static/recovery_prototype.html").read_text(encoding="utf-8")
    (output / "index.html").write_text(template.replace("__RECOVERY_DATA__", encoded), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-comparison", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--candidate-review", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--previous-comparison", type=Path,
                        help="completed prior recovery comparison, required for protocol versions 2 and 3")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        worker(args.worker)
        return 0
    if any(getattr(args, name) is None for name in
           ("source_comparison", "reference", "candidate_review", "protocol", "output_dir")):
        parser.error("source comparison, reference, candidate review, frozen protocol and output are required")
    output = None
    report = dict(format="argos.recovery-prototype-comparison", version=1, state="running")
    try:
        source = args.source_comparison.expanduser().resolve()
        base_path = source / "report.json"
        base = read_json(base_path, 16 * 1024 * 1024)
        if base.get("format") != "argos.tracker-comparison" or base.get("state") != "complete" or base.get("version") != 1:
            raise ValueError("a completed tracker comparison is required")
        protocol = read_json(args.protocol, 1024 * 1024)
        policies = verify_protocol(protocol)
        previous_policies = HELD_POLICIES if protocol["version"] == 3 else POLICIES
        if protocol["version"] in (2, 3) and args.previous_comparison is None:
            raise ValueError("extended recovery requires a previous comparison for legacy parity")
        if protocol["version"] == 1 and args.previous_comparison is not None:
            raise ValueError("previous comparison is only used by protocol versions 2 and 3")
        frozen = {path.resolve(): digest(path) for path in (base_path, args.reference, args.candidate_review, args.protocol)}
        for path in (base_path, args.reference, args.candidate_review, source / "inference.jsonl"):
            if not any(Path(name).resolve() == path.resolve() and sha == digest(path)
                       for name, sha in protocol["input_sha256"].items()):
                raise ValueError("requested evidence is absent from the frozen protocol")
        ref_input = read_json(args.reference, 4 * 1024 * 1024)
        review = read_json(args.candidate_review, 4 * 1024 * 1024)
        windows = [w for w in base["windows"] if w["name"] == ref_input.get("window")]
        if len(windows) != 1:
            raise ValueError("reference must match exactly one original window")
        frames = load_cache(source, base["cache"]["sha256"])
        reference = validate_reference(ref_input, frames, windows[0])
        judgments = validate_judgments(review, frames, reference, reference_sha256=digest(args.reference),
                                       cache_sha256=base["cache"]["sha256"])
        previous = None
        if args.previous_comparison is not None:
            previous_path = args.previous_comparison.expanduser().resolve() / "report.json"
            previous_sha = digest(previous_path)
            if not any(Path(name).resolve() == previous_path and sha == previous_sha
                       for name, sha in protocol["input_sha256"].items()):
                raise ValueError("previous comparison must be bound by the frozen protocol")
            prior = read_json(previous_path, 64 * 1024 * 1024)
            if (prior.get("format") != "argos.recovery-prototype-comparison" or prior.get("state") != "complete"
                    or prior.get("version") != 1 or prior.get("cache_sha256") != base["cache"]["sha256"]
                    or prior.get("windows") != base["windows"]
                    or prior.get("reference", {}).get("sha256") != digest(args.reference)
                    or prior.get("candidate_review_sha256") != digest(args.candidate_review)):
                raise ValueError("previous comparison must use the same completed evidence")
            previous = {}
            for item in prior["runs"]:
                key = item["policy"], item["mode"], item["window"]
                if key in previous:
                    raise ValueError("previous comparison has duplicated conditions")
                previous[key] = item["run"]
            required = {(policy, mode, window["name"]) for policy in previous_policies
                        for mode in MODES for window in base["windows"]}
            if not required <= previous.keys():
                raise ValueError("previous comparison lacks legacy policy conditions")
            frozen[previous_path] = previous_sha
            report["previous_comparison"] = dict(path=str(previous_path), sha256=previous_sha,
                                                   required_legacy_policies=list(previous_policies))
        sources = dict(base["provenance"]["source_sha256"])
        for name, sha in sources.items():
            if name != "argos/perception/continuity_replay.py" and digest(REPO / name) != sha:
                raise ValueError(f"production/comparison dependency changed: {name}")
        for name in ("examples/compare_recovery.py", "examples/validate_continuity.py",
                     "argos/perception/continuity_reference.py", "argos/perception/continuity_replay.py",
                     "argos/perception/recovery_prototype.py", "argos/perception/static/recovery_prototype.html"):
            sources[name] = digest(REPO / name)
        if protocol["version"] == 3:
            sources["argos/perception/candidate_recovery.py"] = digest(REPO / "argos/perception/candidate_recovery.py")
        originals = {}
        for mode in MODES:
            path = source / "passes" / f"repeat-1-bytetrack-{mode}.json"
            frozen[path.resolve()] = digest(path)
            for item in read_json(path, 32 * 1024 * 1024)["windows"]:
                originals[mode, item["name"]] = {k: v for k, v in item.items() if k != "name"}
        output = create_output(args.output_dir, source)
        (output / "passes").mkdir()
        report.update(source=str(source), source_report_sha256=digest(base_path),
                      cache_sha256=base["cache"]["sha256"], model=base["model"], source_sha256=sources,
                      protocol=dict(path=str(args.protocol.resolve()), sha256=digest(args.protocol), contents=protocol),
                      reference={**{k: ref_input[k] for k in ("window", "start", "end", "review_status", "human_validated")},
                                 "sha256": digest(args.reference)}, candidate_review_sha256=digest(args.candidate_review),
                      windows=base["windows"], configuration=dict(policies=list(policies), modes=list(MODES),
                        measured_repeats=3, warmup_processes_per_variant=1, threads=base["configuration"]["threads"],
                        max_hz=base["configuration"]["max_hz"], process_order="rotate policies each repeat; recorded then simulated"))
        write_json(output / "report.json", report)
        passes = []
        environment = dict(os.environ)
        for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            environment[name] = str(base["configuration"]["threads"])
        for repeat in range(4):
            order = policies[repeat % len(policies):] + policies[:repeat % len(policies)]
            for mode in MODES:
                for policy in order:
                    prefix = f"{'warmup' if repeat == 0 else 'repeat-' + str(repeat)}-{policy}-{mode}"
                    result_path = output / "passes" / f"{prefix}.json"
                    job_path = output / "passes" / f"{prefix}-job.json"
                    # No dense-review annotations reach the worker. The window
                    # retains only the original explicit initial-selection box.
                    write_json(job_path, dict(source=str(source), cache_sha256=base["cache"]["sha256"],
                        windows=base["windows"], policy=policy, mode=mode, result=str(result_path),
                        threads=base["configuration"]["threads"], max_hz=base["configuration"]["max_hz"]))
                    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(job_path)],
                                               cwd=REPO, env=environment, capture_output=True, text=True, check=False)
                    if completed.returncode:
                        raise RuntimeError(f"replay worker failed: {prefix}: {completed.stderr[-4000:]}")
                    result = read_json(result_path, 32 * 1024 * 1024)
                    if repeat:
                        passes.append(result)
                    print(f"Completed {prefix}", flush=True)
        report["runs"], report["costs"] = collect_runs(passes, originals, reference, judgments, ref_input,
                                                     policies=policies, previous=previous,
                                                     previous_policies=previous_policies)
        report["limits"] = [
            "Offline prototype on previously examined windows; no independent scene or dense human ground truth.",
            "Labels are used only for evaluation, never provided to the replay worker or policy.",
            "Dense reviewed identity-class agreement covers only the stated reference span; other times are software states only.",
            "Similar-looking overlapping people remain an unresolved identity risk; synthetic controls cannot prove person identity.",
            "Predictions only gate actual measurements; unchanged freshness, deadline and dry consumer still apply.",
            "Held motion uses a fixed assumed error margin, not calibrated probabilistic uncertainty or proof of identity.",
            "Candidate qualification starts only in a lost/paused multi-strong episode; solo native-ID authority is unchanged.",
            "Stable native-ID reuse and observationally indistinguishable people can still produce wrong-person recovery.",
            "CPU/RSS are desktop replay costs, excluding detector and IPC; owner costs do not delay the simulated clock.",
            "No active model, production tracker/preview, service, radio or flight behavior changed."]
        verify_protocol(protocol)
        if any(digest(path) != sha for path, sha in frozen.items()):
            raise ValueError("frozen evidence changed during comparison")
        if any(digest(REPO / name) != sha for name, sha in sources.items()):
            raise ValueError("implementation changed during comparison")
        load_cache(source, base["cache"]["sha256"])
        report["state"] = "complete"
        save_viewer(output, report, frames, reference, judgments)
        write_json(output / "report.json", report)
        print(f"Recovery comparison saved: {output / 'index.html'}", flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        if output is not None:
            report.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
            write_json(output / "report.json", report)
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
