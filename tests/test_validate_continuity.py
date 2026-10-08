"""Evidence preservation and exact counterfactual orchestration without inference."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples import validate_continuity as cli


BOX = [.2, .1, .2, .6]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def native_run():
    return dict(events=[dict(kind="selection", at=.5, track_id=1, accepted=True),
                        dict(kind="association", at=1.01, associations=[dict(detection_index=0, track_id=1)]),
                        dict(kind="preview_transition", at=.5, phase="tracking", target_id=1),
                        dict(kind="consumer_transition", at=.5, valid=True)],
        frame_decisions=[dict(sequence=1, received_at=0., status="not_replayed", reason="not_in_log"),
                         dict(sequence=2, received_at=1., recorded_observation_at=1.01,
                              worker_ms=3., status="accepted", reason="analyzed", delivered_at=1.01,
                              image_age_s=.01, detections=[dict(box=list(BOX), confidence=s, track_id=i + 1)
                                                         for i, s in enumerate((.9, .8, .7))],
                              appearance_available=[True, True, True],
                              association=dict(native_track_ids=[1, 2, 3], native_detection_indices=[0, 1, 2],
                                               tracker_wall_ms=.2, tracker_cpu_ms=.3),
                              preview=dict(phase="tracking", target_id=1), consumer=dict(valid=True)),
                         dict(sequence=3, received_at=1.1, recorded_observation_at=1.11,
                              worker_ms=3., status="accepted", reason="analyzed", delivered_at=1.11,
                              image_age_s=.01, detections=[], appearance_available=[],
                              association=dict(native_track_ids=[], native_detection_indices=[],
                                               tracker_wall_ms=.1, tracker_cpu_ms=.2),
                              preview=dict(phase="paused", target_id=1), consumer=dict(valid=False))],
        summary=dict(mode="recorded", phase_seconds=dict(tracking=.6, paused=.4)))


def test_upstream_projection_excludes_only_downstream_intervention_and_cost():
    baseline = native_run()
    changed = deepcopy(baseline)
    row = changed["frame_decisions"][1]
    row.update(preview=dict(phase="stopped", target_id=None), consumer=dict(valid=False),
               preview_detections=[], preview_detection_indices=[], preview_suppression={"status": "applied"})
    row["association"]["tracker_wall_ms"] = 1000.
    changed["events"].append(dict(kind="preview_suppression", at=1.01, sequence=2))
    changed["events"][-2]["valid"] = False
    assert cli.upstream_projection(changed) == cli.upstream_projection(baseline)


@pytest.mark.parametrize("historical", [False, True])
def test_cli_rebinds_relocated_sources_without_changing_original_report(evidence, historical):
    path = evidence.source / "report.json"
    base = json.loads(path.read_text())
    name = "argos/harness/continuity_replay.py"
    base["provenance"]["source_sha256"][name.replace("harness", "perception") if historical else name] = (
        cli.digest(evidence.repo / name))
    runner = evidence.repo / "examples/diagnose_continuity.py"
    runner.write_text('from argos.harness.continuity_replay import replay\n')
    content = runner.read_bytes()
    if historical:
        content = content.replace(b"argos.harness", b"argos.perception")
    base["provenance"]["source_sha256"]["examples/diagnose_continuity.py"] = hashlib.sha256(content).hexdigest()
    write(path, base)
    frozen = path.read_bytes()
    assert cli.main(evidence.args) == 0
    report = json.loads((evidence.output / "report.json").read_text())
    assert report["source"]["report_sha256"] == hashlib.sha256(frozen).hexdigest()
    assert report["source_sha256"][name] == cli.digest(evidence.repo / name)
    assert name.replace("harness", "perception") not in report["source_sha256"]
    assert report["source_sha256"]["examples/diagnose_continuity.py"] == cli.digest(runner)
    assert path.read_bytes() == frozen


@pytest.mark.parametrize("damage", ["identity", "detection", "receipt", "availability", "admission", "initial_selection"])
def test_upstream_projection_keeps_every_causal_input(damage):
    baseline = native_run()
    changed = deepcopy(baseline)
    row = changed["frame_decisions"][1]
    if damage == "identity":
        row["detections"][0]["track_id"] = 88
    elif damage == "detection":
        row["detections"][0]["confidence"] = .99
    elif damage == "receipt":
        row["received_at"] += .01
    elif damage == "availability":
        row["delivered_at"] += .01
    elif damage == "admission":
        row["status"] = "not_analyzed"
    else:
        changed["events"][0]["track_id"] = 99
    assert cli.upstream_projection(changed) != cli.upstream_projection(baseline)


def test_removed_selected_id_uses_preceding_delivery_not_current_recovery():
    run = native_run()
    earlier = deepcopy(run["frame_decisions"][1])
    earlier["sequence"], earlier["received_at"] = 0, .5
    earlier["preview"]["target_id"] = 2
    run["frame_decisions"].insert(0, earlier)
    current = run["frame_decisions"][2]
    current["preview_suppression"] = dict(suppressed_detection_indices=[1])
    current["preview"]["target_id"] = 1
    risks = cli.removed_previous_targets(run)
    assert [(r["sequence"], r["previous_selected_track_id"], r["removed_detection_indices"]) for r in risks] == [(2, 2, [1])]
    assert "intervening state reads may differ" in risks[0]["meaning"]


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    source, output, repo = tmp_path / "comparison", tmp_path / "validation", tmp_path / "repo"
    paths = ["examples/validate_continuity.py", "argos/perception/continuity_reference.py",
             "argos/harness/continuity_replay.py", "argos/perception/tracker_comparison.py",
             "argos/harness/continuity_provenance.py",
             "argos/perception/static/continuity_validation.html", "argos/perception/image_tracks.py"]
    for path in paths:
        destination = repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"frozen {path}\n")
    monkeypatch.setattr(cli, "REPO", repo)
    monkeypatch.setattr(cli, "__file__", str(repo / paths[0]))
    frames = []
    for sequence, at in ((1, 0.), (2, 1.), (3, 1.1)):
        jpeg = f"original-jpeg-{sequence}".encode()
        path = source / "frames" / f"{sequence}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg)
        frames.append(dict(sequence=sequence, received_at=at, available_at=at + .01, worker_ms=3.,
                           inference_ms=1., width=640, height=480, sha256=hashlib.sha256(jpeg).hexdigest(),
                           appearances=[None] * (3 if sequence == 2 else 0),
                           detections=[dict(box=list(BOX), confidence=s) for s in (.9, .8, .7)] if sequence == 2 else []))
    cache = source / "inference.jsonl"
    cache.write_text("".join(json.dumps(frame) + "\n" for frame in frames))
    window = dict(name="loss", start=0., end=2., selection=dict(at=.5, box=list(BOX)))
    base = dict(format="argos.tracker-comparison", version=1, state="complete", windows=[window],
                cache=dict(sha256=cli.digest(cache)), model=dict(sha256="model-frozen-sha"),
                configuration=dict(max_hz=10), provenance=dict(source_sha256={
                    "argos/perception/image_tracks.py": cli.digest(repo / "argos/perception/image_tracks.py")}))
    write(source / "report.json", base)
    for tracker in ("image", "bytetrack"):
        for mode in ("recorded", "simulated"):
            write(source / "passes" / f"repeat-1-{tracker}-{mode}.json",
                  dict(windows=[dict(name="loss", **native_run())]))
    reference_path, review_path = tmp_path / "reference.json", tmp_path / "candidate-review.json"
    reference = dict(format="argos.continuity.dense-reference", version=1, window="loss",
        review_status="assistant_reviewed", human_validated=False, reviewer="assistant-test", start=1., end=1.1,
        frames=[dict(sequence=f["sequence"], received_at=f["received_at"], sha256=f["sha256"],
                     review_status="assistant_reviewed", other_people=[],
                     target=dict(identity="person-1", visibility="visible", spatial_quality="clear", box=list(BOX)))
                for f in frames[1:]])
    write(reference_path, reference)
    review = dict(format="argos.continuity.detection-review", version=1, review_status="assistant_reviewed",
        human_validated=False, reviewer="assistant-test", reference_sha256=cli.digest(reference_path),
        cache_sha256=cli.digest(cache), frames=[dict(sequence=f["sequence"], frame_sha256=f["sha256"],
            detections=[dict(detection_index=i, **deepcopy(d), classification="target" if i < 2 else "not_person",
                             person_id="person-1" if i < 2 else None, evidence_id=f"evidence-{f['sequence']}-{i}")
                        for i, d in enumerate(f["detections"])]) for f in frames[1:]])
    write(review_path, review)
    calls = []
    def fake_replay(inputs, **kwargs):
        calls.append(deepcopy(kwargs))
        run = native_run()
        masks = kwargs["preview_suppressions"]
        if masks is not None:
            removed = [m["detection_index"] for m in masks]
            row = run["frame_decisions"][1]
            retained = [i for i in range(3) if i not in removed]
            row.update(preview_detection_indices=retained,
                       preview_detections=[deepcopy(row["detections"][i]) for i in retained],
                       preview_suppression=dict(suppressed_detection_indices=removed))
        return run
    monkeypatch.setattr(cli, "replay", fake_replay)
    monkeypatch.setattr(cli, "save_viewer", lambda *args: None)
    def no_model(*args, **kwargs):
        raise AssertionError("validation must not initialize inference")
    from argos.perception import yolox
    monkeypatch.setattr(yolox, "YoloXPersonDetector", no_model)
    args = ["--source-comparison", str(source), "--reference", str(reference_path),
            "--candidate-review", str(review_path), "--output-dir", str(output)]
    return SimpleNamespace(args=args, source=source, output=output, repo=repo, frames=frames, calls=calls,
        reference_path=reference_path, review_path=review_path, reference=reference, review=review,
        replay=fake_replay, base=base)


def test_cli_runs_sixteen_conditions_twice_without_inference_or_evidence_mutation(evidence):
    source_hashes = {p: cli.digest(p) for p in evidence.source.rglob("*") if p.is_file()}
    assert cli.main(evidence.args) == 0
    report = json.loads((evidence.output / "report.json").read_text())
    assert report["state"] == "complete"
    assert len(report["runs"]) == 16 and len(evidence.calls) == 32
    assert all(row["semantic_repeatable"] and row["upstream_unchanged"] for row in report["runs"])
    assert report["reference"]["human_validated"] is False
    assert report["reference"]["source_images"] == 2
    assert [r["detection_index"] for r in report["masks"]["duplicates_only"]] == [1]
    assert [r["detection_index"] for r in report["masks"]["false_positives_only"]] == [2]
    assert source_hashes == {p: cli.digest(p) for p in source_hashes}


@pytest.mark.parametrize("damage", ["missing_frame", "wrong_hash", "human_status", "before_selection", "wrong_cache"])
def test_bad_reference_fails_before_creating_output_or_running_replay(evidence, damage):
    if damage == "missing_frame":
        evidence.reference["frames"].pop()
    elif damage == "wrong_hash":
        evidence.reference["frames"][0]["sha256"] = "f" * 64
    elif damage == "human_status":
        evidence.reference["human_validated"] = True
    elif damage == "before_selection":
        evidence.reference["start"] = .4
    else:
        evidence.review["cache_sha256"] = "f" * 64
    write(evidence.reference_path, evidence.reference)
    evidence.review["reference_sha256"] = cli.digest(evidence.reference_path)
    write(evidence.review_path, evidence.review)
    with pytest.raises(SystemExit) as exc:
        cli.main(evidence.args)
    assert exc.value.code == 1
    assert not evidence.output.exists() and evidence.calls == []


def test_current_production_dependency_change_aborts_before_replay(evidence):
    (evidence.repo / "argos/perception/image_tracks.py").write_text("unapproved tracker change")
    with pytest.raises(SystemExit):
        cli.main(evidence.args)
    report = json.loads((evidence.output / "report.json").read_text())
    assert report["state"] == "failed" and "dependency changed" in report["error"]
    assert evidence.calls == []


@pytest.mark.parametrize("damage", ["baseline", "upstream", "repeat"])
def test_outcome_or_association_changes_abort_instead_of_claiming_comparison(evidence, monkeypatch, damage):
    count = 0
    def bad_replay(inputs, **kwargs):
        nonlocal count
        count += 1
        run = evidence.replay(inputs, **kwargs)
        if damage == "baseline" or damage == "upstream" and kwargs["preview_suppressions"] is not None:
            run["frame_decisions"][1]["detections"][0]["track_id"] = 88
        elif damage == "repeat" and count % 2 == 0:
            run["frame_decisions"][1]["preview"]["phase"] = "stopped"
        return run
    monkeypatch.setattr(cli, "replay", bad_replay)
    with pytest.raises(SystemExit):
        cli.main(evidence.args)
    report = json.loads((evidence.output / "report.json").read_text())
    assert report["state"] == "failed"
    assert ("no longer matches" if damage == "baseline" else "changed association" if damage == "upstream"
            else "not repeatable") in report["error"]


@pytest.mark.parametrize("failure", [RuntimeError("native failure"), KeyboardInterrupt()])
def test_failed_or_interrupted_run_keeps_explicit_state_and_originals(evidence, monkeypatch, failure):
    original = (evidence.source / "inference.jsonl").read_bytes()
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(cli, "replay", fail)
    with pytest.raises(SystemExit):
        cli.main(evidence.args)
    report = json.loads((evidence.output / "report.json").read_text())
    assert report["state"] == ("interrupted" if isinstance(failure, KeyboardInterrupt) else "failed")
    assert (evidence.source / "inference.jsonl").read_bytes() == original


@pytest.mark.parametrize("changed", ["jpeg", "cache", "source_report", "reference", "candidate_review", "code", "baseline_pass"])
def test_evidence_change_during_run_is_detected_before_completion(evidence, monkeypatch, changed):
    paths = dict(jpeg=evidence.source / "frames/3.jpg", cache=evidence.source / "inference.jsonl",
                 source_report=evidence.source / "report.json", reference=evidence.reference_path,
                 candidate_review=evidence.review_path,
                 code=evidence.repo / "argos/perception/image_tracks.py",
                 baseline_pass=evidence.source / "passes/repeat-1-image-recorded.json")
    def mutate(inputs, **kwargs):
        run = evidence.replay(inputs, **kwargs)
        paths[changed].write_bytes(b"replacement")
        return run
    monkeypatch.setattr(cli, "replay", mutate)
    with pytest.raises(SystemExit):
        cli.main(evidence.args)
    report = json.loads((evidence.output / "report.json").read_text())
    assert report["state"] == "failed"
    assert ("source image changed" if changed == "jpeg" else "frozen evidence or implementation changed") in report["error"]


def test_existing_output_is_not_overwritten(evidence):
    evidence.output.mkdir()
    marker = evidence.output / "report.json"
    marker.write_bytes(b"previous artifact, preserve exactly")
    with pytest.raises(SystemExit):
        cli.main(evidence.args)
    assert marker.read_bytes() == b"previous artifact, preserve exactly"
    assert evidence.calls == []


def test_output_cannot_be_created_inside_source_comparison(evidence):
    forbidden = evidence.source / "new-output"
    args = evidence.args[:-1] + [str(forbidden)]
    with pytest.raises(SystemExit):
        cli.main(args)
    assert not forbidden.exists()
    assert evidence.calls == []
