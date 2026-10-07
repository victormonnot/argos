"""Recovery comparison must preserve evidence and expose negative outcomes."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples import compare_recovery as cli


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def sample_run():
    return dict(events=[dict(kind="selection", at=.1, accepted=True, track_id=1),
                        dict(kind="preview_transition", at=.1, phase="tracking")],
                frame_decisions=[dict(sequence=1, received_at=0., status="accepted", delivered_at=.01,
                                     detections=[dict(track_id=1, box=[.2, .2, .1, .5], confidence=.9)],
                                     preview=dict(phase="tracking", target_id=1), consumer=dict(valid=True))],
                summary=dict(phase_seconds=dict(tracking=.9), dry_consumer_valid_seconds=.8))


def measured_pass(policy, mode, run=None):
    value = deepcopy(sample_run() if run is None else run)
    value["summary"]["execution_cost"] = {"preview_total_cpu_ms": 1.}
    return dict(policy=policy, mode=mode, windows=[dict(name="unreviewed", **value)],
                cost=dict(process_wall_s=.1, process_cpu_s=.08, peak_rss_bytes=1024,
                          scope="synthetic fixture"))


@pytest.fixture
def passes():
    return [measured_pass(policy, mode) for policy in cli.POLICIES for mode in cli.MODES for _ in range(3)]


def collect(passes):
    originals = {(mode, "unreviewed"): sample_run() for mode in cli.MODES}
    return cli.collect_runs(passes, originals, {}, {}, dict(window="reviewed", start=1., end=2.))


def test_repeatable_results_keep_unreviewed_spans_outside_dense_denominators(passes):
    rows, costs = collect(passes)
    assert len(rows) == len(costs) == 6
    assert all(row["evaluation"] is None and row["upstream_unchanged"] for row in rows)


@pytest.mark.parametrize("mutation", ["repeat", "raw_id", "timing", "initial_selection", "default_changed"])
def test_changed_evidence_or_default_aborts(passes, mutation):
    affected = passes[6:9]  # motion/recorded
    if mutation == "repeat":
        affected = affected[:1]
    if mutation == "default_changed":
        affected = passes[:3]
    for item in affected:
        run = item["windows"][0]
        if mutation == "raw_id":
            run["frame_decisions"][0]["detections"][0]["track_id"] = 42
        elif mutation == "timing":
            run["frame_decisions"][0]["delivered_at"] = .1
        elif mutation == "initial_selection":
            run["events"][0]["track_id"] = 42
        else:
            run["summary"]["dry_consumer_valid_seconds"] = 0.
    with pytest.raises(ValueError):
        collect(passes)


def test_negative_prototype_outcome_is_preserved(passes):
    for item in passes:
        if item["policy"] != "current":
            item["windows"][0]["summary"]["dry_consumer_valid_seconds"] = 0.
    rows, _ = collect(passes)
    assert all(row["run"]["summary"]["dry_consumer_valid_seconds"] == 0.
               for row in rows if row["policy"] != "current")


def test_cost_changes_do_not_count_as_semantic_instability(passes):
    passes[0]["windows"][0]["summary"]["execution_cost"]["preview_total_cpu_ms"] = 999.
    assert len(collect(passes)[0]) == 6


def test_missing_measured_repeat_fails(passes):
    with pytest.raises(ValueError, match="three"):
        collect(passes[:-1])


def protocol(bindings):
    return dict(format="argos.recovery-prototype.protocol", version=1, policies=list(cli.POLICIES),
                clocks=list(cli.MODES), trackers=["bytetrack"], evaluation=dict(repeats=3),
                motion=dict(horizontal_only=True, history_s=.7, min_selected_distinct_measurements=2,
                            horizon_s=.5, residual_max_dx=.25, predicted_commands=False,
                            velocity="last two selected measurements; clamp +/-1 normalized width/s",
                            fallback="unchanged static geometry when insufficient/old history",
                            appearance_vertical_scale_gates="unchanged production reference and thresholds"),
                duplicates=dict(iou_min=.85, appearance_similarity_min=.95, all_strong_pairwise_clique=True,
                                annotations_available_to_policy=False,
                                priority=["selected ID", "pending recovery ID", "confidence", "source index"],
                                missing_appearance="no grouping"), input_sha256=bindings)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    jpeg = b"original preserved"
    (source / "frames").mkdir()
    (source / "frames/1.jpg").write_bytes(jpeg)
    frame = dict(sequence=1, received_at=0., sha256=cli.digest(source / "frames/1.jpg"),
                 detections=[], appearances=[], width=640, height=480)
    write(source / "inference.jsonl", frame)
    window = dict(name="unreviewed", start=0., end=1., selection=dict(at=.1, box=[.2, .2, .1, .5]))
    base = dict(format="argos.tracker-comparison", version=1, state="complete", windows=[window],
                cache=dict(sha256=cli.digest(source / "inference.jsonl")), model={},
                provenance=dict(source_sha256={}), configuration=dict(threads=1, max_hz=10))
    write(source / "report.json", base)
    for mode in cli.MODES:
        write(source / "passes" / f"repeat-1-bytetrack-{mode}.json",
              dict(windows=[dict(name="unreviewed", **sample_run())]))
    ref, review, frozen = (tmp_path / name for name in ("reference.json", "review.json", "protocol.json"))
    write(ref, dict(window="unreviewed", start=.5, end=1., review_status="assistant_reviewed", human_validated=False))
    write(review, {})
    write(frozen, protocol({str(p): cli.digest(p) for p in (source / "report.json", source / "inference.jsonl", ref, review)}))
    monkeypatch.setattr(cli, "validate_reference", lambda *a: {})
    monkeypatch.setattr(cli, "validate_judgments", lambda *a, **kw: {})
    monkeypatch.setattr(cli, "evaluate_replay", lambda *a, **kw: dict(counts={}))
    jobs = []
    def fake_subprocess(command, **kwargs):
        job = json.loads(Path(command[-1]).read_text())
        jobs.append(deepcopy(job))
        write(Path(job["result"]), measured_pass(job["policy"], job["mode"]))
        return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(cli.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(cli, "save_viewer", lambda *a: None)
    args = ["--source-comparison", str(source), "--reference", str(ref), "--candidate-review", str(review),
            "--protocol", str(frozen), "--output-dir", str(output)]
    return dict(args=args, source=source, output=output, ref=ref, protocol=frozen, jobs=jobs)


def test_cli_workers_receive_no_review_or_oracle_paths(evidence):
    assert cli.main(evidence["args"]) == 0
    assert len(evidence["jobs"]) == 24
    assert all(set(job) == {"source", "cache_sha256", "windows", "policy", "mode", "result", "threads", "max_hz"}
               for job in evidence["jobs"])
    report = json.loads((evidence["output"] / "report.json").read_text())
    assert report["state"] == "complete"
    assert len(report["runs"]) == 6


def test_protocol_bound_evidence_cannot_change(evidence):
    evidence["ref"].write_text("{}")
    with pytest.raises(SystemExit):
        cli.main(evidence["args"])
    assert not evidence["output"].exists()


def test_failed_subprocess_leaves_failure_report(evidence, monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stderr="fixture failure"))
    with pytest.raises(SystemExit):
        cli.main(evidence["args"])
    assert json.loads((evidence["output"] / "report.json").read_text())["state"] == "failed"


def test_existing_output_is_never_replaced(evidence):
    evidence["output"].mkdir()
    keep = evidence["output"] / "keep.txt"
    keep.write_text("preserved")
    with pytest.raises(SystemExit):
        cli.main(evidence["args"])
    assert keep.read_text() == "preserved"


@pytest.mark.parametrize("section,key,value", [("motion", "horizon_s", .8),
    ("motion", "predicted_commands", True), ("duplicates", "iou_min", .4),
    ("duplicates", "annotations_available_to_policy", True),
    ("motion", "velocity", "unbounded"), ("duplicates", "priority", ["confidence"]),
    ("motion", "fallback", "always extrapolate")])
def test_different_protocol_is_not_silently_run(evidence, section, key, value):
    data = json.loads(evidence["protocol"].read_text())
    data[section][key] = value
    write(evidence["protocol"], data)
    with pytest.raises(SystemExit):
        cli.main(evidence["args"])
    assert not evidence["output"].exists()
