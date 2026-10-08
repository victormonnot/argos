"""Controlled selection-layer qualification, without video or identity truth input.

Synthetic appearances and native IDs are prescribed inputs, not detector or
tracker outputs. Symbolic person labels remain exclusively in the evaluator.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import time

from argos.backends.yaw_stream_source import YawValidator
from argos.console.yaw_preview import FRAME_MAX_AGE, MIN_CONFIDENCE, YawPreview, _observation
from argos.perception.recovery_prototype import make_preview

POLICIES = ("current", "motion_held", "motion_held_candidates")
SCHEMA = "argos.synthetic-multi-person.v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def descriptor(cosine=1.):
    return [cosine, math.sqrt(max(0., 1. - cosine * cosine))] + [0.] * 206


def _person(identity=7, truth="A", center=.4, appearance=1., confidence=.9):
    return dict(track_id=identity, truth=truth, box=[center - .06, .2, .12, .4],
                appearance=None if appearance is None else descriptor(appearance),
                confidence=confidence)


def _frame(sequence, at, people):
    return dict(at=at, observation=dict(run_id="synthetic", video_id="boxes", sequence=sequence,
        received_at=at, width=640, height=480,
        detections=[{key: deepcopy(p[key]) for key in ("track_id", "box", "confidence")} for p in people],
        appearances=[deepcopy(p["appearance"]) for p in people]),
        evaluator_truth=[p["truth"] for p in people])


def build_manifest():
    """Declare fixed controls and failure examples before any policy execution."""
    scenarios = []
    def add(name, explanation, rows, *, end=2., known_limit=False, pair=None):
        frames = [_frame(1, 1., [_person(center=.35)]),
                  _frame(2, 1.2, [_person(center=.37)])]
        frames.extend(_frame(i + 3, at, people) for i, (at, people) in enumerate(rows))
        scenarios.append(dict(name=name, explanation=explanation, known_limit=known_limit,
            permutation_pair=pair, target_person="A", select_at=1., select_id=7,
            end=end, tick_seconds=.01, frames=frames))
    times = (1.4, 1.5, 1.6, 1.7, 1.8)
    target = lambda identity=9, **kw: _person(identity, "A", **kw)
    other = lambda **kw: _person(11, "B", **kw)
    add("stable_bystander", "Selected native ID remains intact beside another person.",
        [(t, [target(7), other(center=.65, appearance=0.)]) for t in times])
    add("new_id_incompatible_other", "Target changes ID beside appearance-incompatible person.",
        [(t, [target(), other(center=.6, appearance=0.)]) for t in times], pair="unique_order")
    add("new_id_reverse_order_scores", "Same observations with reversed candidate order and confidence priority.",
        [(t, [other(center=.6, appearance=0., confidence=.99), target(confidence=.6)]) for t in times],
        pair="unique_order")
    add("same_id_return_with_other", "After a gap the old ID returns beside another person; confirmation still required.",
        [(1.4, [])] + [(t, [target(7), other(center=.6, appearance=0.)]) for t in times[1:]])
    add("crossing_ambiguous_then_resolves", "Two plausible people pause recovery; incompatible second appearance later resolves it.",
        [(t, [target(), other(center=.48, appearance=.96 if t < 1.6 else 0.)]) for t in times])
    add("target_leaves_other_remains", "Target leaves; visible other person has incompatible appearance.",
        [(t, [other(center=.4, appearance=0.)]) for t in times])
    add("plausible_missing_appearance", "A plausible competitor without appearance prevents disambiguation.",
        [(t, [target(), other(center=.48, appearance=None)]) for t in times])
    add("near_tie_below_threshold", "Winner .90 and plausible rival .86 differ by .04; below-threshold rival still blocks.",
        [(t, [target(appearance=.90), other(center=.48, appearance=.86)]) for t in times])
    add("margin_boundary", "Winner .91 and plausible rival .86 have exactly the fixed .05 separation.",
        [(t, [target(appearance=.91), other(center=.48, appearance=.86)]) for t in times])
    add("compatible_far_geometry", "Appearance-compatible but geometrically impossible rival does not block target.",
        [(t, [target(), other(center=.90, appearance=1.)]) for t in times])
    add("ambiguity_breaks_confirmation", "A second plausible person between two good images resets pending confirmation.",
        [(t, [target()] + ([other(center=.48, appearance=.96)] if t == 1.5 else [])) for t in times])
    add("missing_detections_then_return", "No detection during a short gap; current measurement returns with new ID.",
        [(1.4, [])] + [(t, [target()]) for t in times[1:]])
    add("missing_target_appearance", "No target descriptor initially; only later measured descriptors can confirm.",
        [(t, [target(appearance=None if t < 1.6 else 1.), other(center=.6, appearance=0.)]) for t in times])
    add("repeated_poll_not_confirmation", "Only one new-ID image; repeated fresh owner polls must not confirm.",
        [(1.4, [target()])], end=1.8)
    add("stale_image_stops", "Owner keeps polling one old image past the unchanged .45-second freshness limit.",
        [], end=1.8)
    deadline_times = [round(1.4 + i * .2, 10) for i in range(16)]
    add("ambiguity_expires_at_deadline", "Fresh ambiguous images cannot renew the three-second last-selected deadline.",
        [(t, [target(), other(center=.48, appearance=.96)]) for t in deadline_times], end=4.4)
    add("limit_indistinguishable_replacement", "A departed target is replaced by a different person with identical observable evidence and a new ID.",
        [(t, [_person(9, "B", center=.4, appearance=1.)]) for t in times], known_limit=True)
    add("limit_selected_id_reused", "Tracker reuses the actively selected ID for another person; selection layer still trusts that active ID.",
        [(t, [_person(7, "B", center=.4, appearance=0.)]) for t in times], known_limit=True)
    return dict(schema=SCHEMA, provenance="hand_authored_controlled_metadata_no_real_images",
        ground_truth="symbolic scenario labels, evaluator only; not human annotated video",
        policy_inputs="observation only, copied; no person truth, scenario name, outcome, or annotation",
        scope="selection and actual dry YawValidator only; no detector, association, camera, IPC, transport, or flight",
        owner_clock="prescribed seconds; owner CPU does not delay simulated clock",
        limitations=["Hand-designed controls do not estimate a real-world identity error rate.",
            "Appearance-compatible replacement and active native-ID reuse can select another person; these failures remain in totals.",
            "Nonnegative synthetic 208D unit vectors exercise gates, not actual camera crop appearance quality."],
        policies=list(POLICIES), scenarios=scenarios)


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError("Unsupported synthetic manifest")
    if manifest.get("policies") != list(POLICIES):
        raise ValueError("Synthetic policy contract differs")
    names = set()
    for scenario in manifest.get("scenarios", []):
        if scenario["name"] in names:
            raise ValueError("Duplicate scenario")
        names.add(scenario["name"])
        if scenario["target_person"] != "A" or scenario["tick_seconds"] != .01:
            raise ValueError("Unexpected synthetic target or tick contract")
        if type(scenario["known_limit"]) is not bool:
            raise ValueError("Known-limit marker must be boolean")
        last_at, last_sequence = -1., 0
        for frame in scenario["frames"]:
            obs = frame["observation"]
            if set(obs) != {"run_id", "video_id", "sequence", "received_at", "width", "height", "detections", "appearances"}:
                raise ValueError("Truth or unsupported metadata in observation")
            if any(set(d) != {"track_id", "box", "confidence"} for d in obs["detections"]):
                raise ValueError("Truth or unsupported metadata in detection")
            _observation(obs)
            if not last_at < frame["at"] <= scenario["end"] or obs["received_at"] != frame["at"]:
                raise ValueError("Synthetic timeline must strictly advance")
            if obs["sequence"] <= last_sequence:
                raise ValueError("Synthetic sequence must strictly advance")
            truth = frame["evaluator_truth"]
            if len(truth) != len(obs["detections"]) or any(p not in ("A", "B") for p in truth):
                raise ValueError("Evaluator truth must align with every detection")
            last_at, last_sequence = frame["at"], obs["sequence"]
        first = scenario["frames"][0]
        if first["at"] != scenario["select_at"] or scenario["select_id"] != 7:
            raise ValueError("Initial selection contract differs")
        chosen = [p for d, p in zip(first["observation"]["detections"], first["evaluator_truth"])
                  if d["track_id"] == scenario["select_id"]]
        if chosen != [scenario["target_person"]]:
            raise ValueError("Initial selection must explicitly identify the intended person")
    if not names:
        raise ValueError("No synthetic scenarios")
    return deepcopy(manifest)


def freeze_manifest(path):
    path = Path(path)
    manifest = validate_manifest(build_manifest())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(manifest, stream, indent=2, allow_nan=False)
        stream.write("\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(str(path) + ".sha256").write_text(sha + "\n")
    return sha


def read_manifest(path):
    path = Path(path)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if Path(str(path) + ".sha256").read_text().strip() != sha:
        raise ValueError("Frozen scenario bytes differ from manifest SHA256")
    return validate_manifest(json.loads(raw)), sha


def _snapshot(now, observation, preview):
    # Contract-shaped dry snapshot only: no device, HTTP, or transport is opened.
    receipt = observation["received_at"]
    return dict(schema_version=1, at=now, run_id=observation["run_id"], environment="real",
        reconnecting=None, configuration=dict(environment="real", video_source="device", video_endpoint="/dev/video0"),
        video=dict(source_id=observation["video_id"], source="device", endpoint="/dev/video0", state="recent",
                   received_at=receipt, rx_age_s=now - receipt, age_limit_s=1.),
        vision=dict(configured=True, state="recent", frame_age_s=now - receipt, age_limit_s=1., inference_ms=0.),
        yaw_preview=preview)


def run_scenario(scenario, policy, *, preview_factory=None):
    """Execute immutable synthetic observations; truth is joined only afterward."""
    recovery, diagnostics = [], []
    if preview_factory is None:
        preview = (YawPreview(True, continuous=True, on_recovery=recovery.append) if policy == "current"
                   else make_preview(policy, on_recovery=recovery.append, on_policy=diagnostics.append))
    else:
        preview = preview_factory(policy, on_recovery=recovery.append, on_policy=diagnostics.append)
    validator = YawValidator()
    frames = {round(frame["at"], 10): frame for frame in scenario["frames"]}
    start = scenario["select_at"]
    ticks = {round(start + n * .01, 10) for n in range(round((scenario["end"] - start) / .01) + 1)}
    schedule = sorted(ticks | set(frames) | {scenario["end"]})
    trace, current = [], None
    owner_cpu_ns = owner_wall_ns = 0
    for now in schedule:
        delivered = now in frames
        if delivered:
            current = frames[now]
        observation = deepcopy(current["observation"])
        cpu, wall = time.process_time_ns(), time.perf_counter_ns()
        if delivered:
            preview.observe(observation, now)
        explicit = now == start
        if explicit:
            preview.select(scenario["select_id"], revision=preview.revision, now=now)
        state = preview.state(now)
        owner_cpu_ns += time.process_time_ns() - cpu
        owner_wall_ns += time.perf_counter_ns() - wall
        try:
            consumer = asdict(validator.validate(_snapshot(now, observation, state), now, now,
                explicit_selection=explicit))
        except ValueError as exc:
            consumer = dict(valid=False, value=0, reason="validator_rejected", detail=str(exc))
        # This join is deliberately after observe/select/state and dry validation.
        chosen = next((i for i, d in enumerate(observation["detections"])
                       if d["track_id"] == state["target_id"]), None)
        selected_truth = None if chosen is None else current["evaluator_truth"][chosen]
        fresh_selection = (state["phase"] == "tracking" and chosen is not None
            and observation["detections"][chosen]["confidence"] >= MIN_CONFIDENCE
            and 0 <= now - observation["received_at"] <= FRAME_MAX_AGE)
        trace.append(dict(at=now, delivered=delivered, sequence=observation["sequence"],
            preview=state, consumer=consumer, selected_person=selected_truth if fresh_selection else None,
            fresh_selection=fresh_selection))
    summary = evaluate_trace(trace, scenario["target_person"])
    return dict(scenario=scenario["name"], policy=policy, input_sha256=digest(scenario),
        known_limit=scenario["known_limit"], summary=summary, trace=trace,
        recovery_events=recovery, policy_events=diagnostics,
        cost=dict(owner_cpu_ms=owner_cpu_ns / 1e6, owner_wall_ms=owner_wall_ns / 1e6,
                  scope="preview observe/select/state including diagnostic callbacks; excludes evaluator and dry consumer"))


def evaluate_trace(trace, target_person):
    """Count every case, including known indistinguishable-person failures."""
    measured = [row for row in trace if row["delivered"]]
    fresh = [row for row in measured if row["fresh_selection"]]
    admitted = [row for row in measured if row["consumer"]["valid"]]
    wrong = lambda row: row["selected_person"] is not None and row["selected_person"] != target_person
    phase_seconds = Counter()
    dry_s = wrong_dry_s = 0.
    for left, right in zip(trace, trace[1:]):
        dt = right["at"] - left["at"]
        phase_seconds[left["preview"]["phase"]] += dt
        dry_s += dt * left["consumer"]["valid"]
        wrong_dry_s += dt * left["consumer"]["valid"] * wrong(left)
    pause_at = next((r["at"] for r in trace if r["preview"]["phase"] == "paused"), None)
    recovery_at = next((r["at"] for r in trace if pause_at is not None and r["at"] > pause_at
                        and r["fresh_selection"] and r["selected_person"] == target_person), None)
    consumer_at = next((r["at"] for r in trace if recovery_at is not None and r["at"] >= recovery_at
                        and r["consumer"]["valid"] and r["selected_person"] == target_person), None)
    wrong_rows = [r for r in trace if r["fresh_selection"] and wrong(r)]
    return dict(delivered_images=len(measured), selected_target_images=sum(r["selected_person"] == target_person for r in fresh),
        wrong_person_selected_images=sum(wrong(r) for r in fresh),
        dry_admitted_images=len(admitted), wrong_person_dry_admitted_images=sum(wrong(r) for r in admitted),
        wrong_person_ever_selected=bool(wrong_rows), wrong_person_ever_admitted=any(r["consumer"]["valid"] and wrong(r) for r in trace),
        first_wrong_selection_at=None if not wrong_rows else wrong_rows[0]["at"],
        dry_admitted_seconds=dry_s, wrong_person_dry_admitted_seconds=wrong_dry_s,
        phase_seconds=dict(phase_seconds), first_pause_at=pause_at,
        target_recovery_at=recovery_at, target_consumer_resume_at=consumer_at,
        target_recovery_latency_s=None if recovery_at is None else recovery_at - pause_at,
        target_consumer_latency_s=None if consumer_at is None else consumer_at - pause_at,
        final_phase=trace[-1]["preview"]["phase"], final_consumer_reason=trace[-1]["consumer"]["reason"])


def semantic(result):
    return {key: value for key, value in result.items() if key != "cost"}


def qualify(manifest):
    manifest = validate_manifest(manifest)
    original = canonical(manifest)
    results = []
    for scenario in manifest["scenarios"]:
        for policy in manifest["policies"]:
            first, second = run_scenario(scenario, policy), run_scenario(scenario, policy)
            if canonical(semantic(first)) != canonical(semantic(second)):
                raise RuntimeError("Synthetic repeat semantics differ")
            first["repeat_verified"] = True
            first["repeat_cost"] = second["cost"]
            results.append(first)
    if canonical(manifest) != original:
        raise RuntimeError("Synthetic inputs changed during qualification")
    totals = {}
    for policy in manifest["policies"]:
        selected = [result for result in results if result["policy"] == policy]
        totals[policy] = dict(scenarios=len(selected), known_limit_scenarios=sum(r["known_limit"] for r in selected),
            **{key: sum(r["summary"][key] for r in selected) for key in (
                "wrong_person_selected_images", "wrong_person_dry_admitted_images", "wrong_person_dry_admitted_seconds")},
            scenarios_with_wrong_selection=sum(r["summary"]["wrong_person_ever_selected"] for r in selected),
            scenarios_with_wrong_admission=sum(r["summary"]["wrong_person_ever_admitted"] for r in selected))
    return dict(schema=SCHEMA, status="complete", synthetic=True, manifest_semantic_sha256=digest(manifest),
        limits=manifest["limitations"], scope=manifest["scope"], policies=manifest["policies"],
        headline_includes_known_limits=True, totals=totals, results=results)


def write_viewer(path, report, manifest):
    template = Path(__file__).resolve().parents[1].joinpath("perception/static/multi_person_qualification.html").read_text()
    payload = json.dumps(dict(report=report, manifest=manifest), allow_nan=False).replace("<", "\\u003c")
    Path(path).write_text(template.replace("/*__DATA__*/null", payload))
