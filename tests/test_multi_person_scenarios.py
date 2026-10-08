"""The synthetic qualification must expose failures and keep labels out of policy."""
from copy import deepcopy
import json

import pytest

from argos.console.yaw_preview import YawPreview
from argos.harness.multi_person_scenarios import (
    POLICIES, build_manifest, canonical, evaluate_trace, freeze_manifest, qualify,
    read_manifest, run_scenario, semantic, validate_manifest, write_viewer,
)


def case(name):
    return next(s for s in build_manifest()["scenarios"] if s["name"] == name)


def test_truth_is_not_given_to_preview_and_renaming_later_truth_cannot_change_decisions():
    received = []
    class Spy(YawPreview):
        def observe(self, observation, now):
            received.append(deepcopy(observation))
            return super().observe(observation, now)
    def factory(policy, **kwargs):
        return Spy(True, continuous=True, on_recovery=kwargs["on_recovery"])
    scenario = case("limit_indistinguishable_replacement")
    result = run_scenario(scenario, "current", preview_factory=factory)
    relabeled = deepcopy(scenario)
    for frame in relabeled["frames"][2:]:
        frame["evaluator_truth"] = ["A"]
    other = run_scenario(relabeled, "current", preview_factory=factory)
    assert result["summary"]["wrong_person_selected_images"] > 0
    assert other["summary"]["wrong_person_selected_images"] == 0
    assert [r["preview"] for r in result["trace"]] == [r["preview"] for r in other["trace"]]
    assert [r["consumer"] for r in result["trace"]] == [r["consumer"] for r in other["trace"]]
    for obs in received:
        assert set(obs) == {"run_id", "video_id", "sequence", "received_at", "width", "height", "detections", "appearances"}
        assert all(set(d) == {"track_id", "box", "confidence"} for d in obs["detections"])


@pytest.mark.parametrize("name", ["limit_indistinguishable_replacement", "limit_selected_id_reused"])
@pytest.mark.parametrize("policy", POLICIES)
def test_known_counterexamples_are_counted_as_real_failures_of_observation_contract(name, policy):
    result = run_scenario(case(name), policy)
    summary = result["summary"]
    assert result["known_limit"]
    assert summary["wrong_person_ever_selected"]
    assert summary["wrong_person_ever_admitted"]
    assert summary["wrong_person_selected_images"] > 0
    assert summary["wrong_person_dry_admitted_images"] > 0
    assert summary["wrong_person_dry_admitted_seconds"] > 0


@pytest.mark.parametrize("policy", POLICIES)
def test_one_new_image_repeated_polls_cannot_confirm_or_renew_evidence(policy):
    result = run_scenario(case("repeated_poll_not_confirmation"), policy)
    pending = [r for r in result["trace"] if r["at"] >= 1.4]
    assert {r["preview"]["phase"] for r in pending} == {"paused"}
    assert not any(r["consumer"]["valid"] for r in pending)
    assert {r["preview"]["recovery_deadline_at"] for r in pending} == {4.2}
    assert result["summary"]["delivered_images"] == 3
    assert result["summary"]["selected_target_images"] == 2


@pytest.mark.parametrize("policy", POLICIES)
def test_stale_image_stops_without_new_receipt(policy):
    result = run_scenario(case("stale_image_stops"), policy)
    assert result["summary"]["final_phase"] == "stopped"
    assert all(not r["consumer"]["valid"] for r in result["trace"] if r["at"] >= 1.66)


def test_candidate_order_and_confidence_priority_do_not_choose_another_identity():
    left = run_scenario(case("new_id_incompatible_other"), "motion_held_candidates")
    right = run_scenario(case("new_id_reverse_order_scores"), "motion_held_candidates")
    projection = lambda r: [(row["preview"]["phase"], row["preview"]["target_id"],
                            row["consumer"]["valid"], row["selected_person"]) for row in r["trace"]]
    assert projection(left) == projection(right)
    assert left["summary"]["final_phase"] == "tracking"


@pytest.mark.parametrize("name", ["plausible_missing_appearance", "near_tie_below_threshold", "target_leaves_other_remains"])
def test_negative_controls_cannot_admit_new_person(name):
    result = run_scenario(case(name), "motion_held_candidates")
    assert result["summary"]["final_phase"] == "paused"
    assert result["summary"]["wrong_person_selected_images"] == 0
    assert not any(r["consumer"]["valid"] for r in result["trace"] if r["at"] >= 1.4)


def test_ambiguity_deadline_uses_last_selected_receipt_even_with_fresh_images():
    result = run_scenario(case("ambiguity_expires_at_deadline"), "motion_held_candidates")
    assert result["summary"]["final_phase"] == "stopped"
    assert all(not row["consumer"]["valid"] for row in result["trace"] if row["at"] >= 1.4)
    terminal = next(row for row in result["trace"] if row["preview"]["phase"] == "stopped")
    assert terminal["at"] == 4.2
    assert terminal["preview"]["frame_age_s"] == 0.


def test_ambiguity_resets_confirmation_and_consumer_requires_its_own_image():
    result = run_scenario(case("ambiguity_breaks_confirmation"), "motion_held_candidates")
    measured = {row["at"]: row for row in result["trace"] if row["delivered"]}
    assert measured[1.6]["preview"]["phase"] == "paused"
    assert measured[1.7]["preview"]["phase"] == "tracking"
    assert not measured[1.7]["consumer"]["valid"]
    assert measured[1.8]["consumer"]["valid"]


def test_freeze_binds_exact_bytes_and_does_not_overwrite(tmp_path):
    path = tmp_path / "manifest.json"
    sha = freeze_manifest(path)
    manifest, actual = read_manifest(path)
    assert sha == actual and len(manifest["scenarios"]) == 18
    with pytest.raises(FileExistsError):
        freeze_manifest(path)
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="SHA256"):
        read_manifest(path)


@pytest.mark.parametrize("mutation", ["truth_in_observation", "truth_in_detection", "unaligned_truth", "wrong_initial_target"])
def test_rejects_manifest_identity_leak_or_bad_evaluator_join(mutation):
    manifest = build_manifest()
    frame = manifest["scenarios"][0]["frames"][0]
    if mutation == "truth_in_observation":
        frame["observation"]["truth"] = "A"
    elif mutation == "truth_in_detection":
        frame["observation"]["detections"][0]["truth"] = "A"
    elif mutation == "unaligned_truth":
        frame["evaluator_truth"] = []
    else:
        frame["evaluator_truth"] = ["B"]
    with pytest.raises(ValueError):
        validate_manifest(manifest)


def test_all_repeats_preserve_inputs_and_headline_keeps_known_limit_failures(tmp_path):
    manifest = build_manifest()
    before = canonical(manifest)
    report = qualify(manifest)
    assert canonical(manifest) == before
    assert len(report["results"]) == 54
    assert all(r["repeat_verified"] for r in report["results"])
    assert report["headline_includes_known_limits"]
    for policy in POLICIES:
        total = report["totals"][policy]
        assert total["scenarios"] == 18 and total["known_limit_scenarios"] == 2
        assert total["scenarios_with_wrong_selection"] >= 2
        assert total["scenarios_with_wrong_admission"] >= 2
    output = tmp_path / "index.html"
    write_viewer(output, report, manifest)
    assert "SYNTHÉTIQUE" in output.read_text()
    assert "/*__DATA__*/null" not in output.read_text()
    assert "limit_indistinguishable_replacement" in output.read_text()


def test_cli_records_source_freeze_and_refuses_output_reuse(tmp_path, capsys):
    from examples.qualify_multi_person import main
    manifest_path = tmp_path / "scenarios.json"
    main(["--freeze-manifest", str(manifest_path)])
    output = tmp_path / "out"
    main(["--scenario-manifest", str(manifest_path), "--output-dir", str(output)])
    report = json.loads((output / "report.json").read_text())
    freeze = json.loads((output / "implementation-freeze.json").read_text())
    assert report["implementation_sha256"] == freeze["implementation_sha256"]
    assert report["manifest_file_sha256"] == freeze["manifest_file_sha256"]
    assert report["implementation_unchanged_after_execution"] is True
    assert "argos/harness/multi_person_scenarios.py" in report["implementation_sha256"]
    assert "argos/perception/multi_person_scenarios.py" not in report["implementation_sha256"]
    assert "argos/perception/candidate_recovery.py" in report["implementation_sha256"]
    assert "argos/backends/yaw_stream_source.py" in report["implementation_sha256"]
    assert "argos/perception/static/multi_person_qualification.html" in report["implementation_sha256"]
    original = (output / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        main(["--scenario-manifest", str(manifest_path), "--output-dir", str(output)])
    assert (output / "report.json").read_bytes() == original
