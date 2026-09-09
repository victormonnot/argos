"""Offline policy comparison executes the real lifecycle without resurrecting loss."""
import json
from pathlib import Path

import pytest

from argos.console import framing as lifecycle
from examples.replay_framing import Candidate, Window, _parameters, compare, main, replay


def frame(at, sequence, *, confidence=.9, identity=7, received_at=None, detections=None,
          box=None, video_id="camera"):
    return {"kind": "frame", "at": at, "sequence": sequence,
            "received_at": at if received_at is None else received_at,
            "run_id": "run", "video_id": video_id,
            "detections": [{"track_id": identity, "confidence": confidence,
                            "box": [.45, .4, .1, .2] if box is None else box}]
            if detections is None else detections}


def tick(at, **kwargs):
    return {"kind": "tick", "at": at, **kwargs}


def test_grid_changes_continuation_but_never_loosens_engagement():
    records = [frame(0., 1, confidence=.47), tick(1.)]
    report = compare(records, [Window("episode", 0., 1., 7)])
    assert len(report["results"]) == 9
    assert all(not r["engaged"] and "confidence" in r["engagement_error"]
               for r in report["results"])


def test_confidence_candidate_can_continue_same_id_but_baseline_latches():
    records = [frame(0., 1), frame(.2, 2, confidence=.47), tick(.2),
               frame(.4, 3, confidence=.47), tick(.4), tick(.56),
               frame(.6, 4), tick(.6), tick(1.)]
    window = Window("one engagement", 0., 1., 7)
    baseline = replay(records, window, Candidate(.5, .35))
    candidate = replay(records, window, Candidate(.45, .35))
    assert baseline["first_loss"]["at"] == .56
    assert "confidence" in baseline["first_loss"]["reason"]
    assert baseline["duration_s"]["takeover"] == pytest.approx(.44)
    assert baseline["pause_recoveries"] == 0  # the .6 good frame cannot resurrect it
    assert candidate["first_loss"] is None
    assert candidate["duration_s"]["valid"] == 1.


def test_longer_pause_recovers_only_fresh_same_identity_and_outputs_stay_zero():
    records = [frame(0., 1), frame(.2, 2, detections=[]), tick(.2),
               frame(.4, 3, detections=[]), tick(.4), tick(.56),
               frame(.6, 4), tick(.6), frame(.8, 5), tick(.8), tick(1.)]
    result = replay(records, Window("pause", 0., 1., 7), Candidate(.5, .6))
    assert result["first_loss"] is None and result["pause_recoveries"] == 1
    assert result["duration_s"]["paused"] == pytest.approx(.4)
    pause = next(t for t in result["transitions"] if t["phase"] == "paused")
    assert all(value == 0 for value in pause["axes"].values())


@pytest.mark.parametrize("change,reason", [
    ({"identity": 8}, "different person IDs"),
    ({"video_id": "other"}, "Camera source changed"),
    ({"box": [0., .4, .1, .2]}, "image edge"),
    ({"box": [.45, .4, .1, .7]}, "Invalid analyzed image"),
])
def test_geometry_identity_and_source_guards_are_not_relaxed(change, reason):
    records = [frame(0., 1), frame(.2, 2, **change), tick(.2), tick(1.)]
    result = replay(records, Window("guard", 0., 1., 7), Candidate(.4, .8))
    assert result["first_loss"]["at"] == .2
    assert reason in result["first_loss"]["reason"]


def test_repeated_frames_never_refresh_staleness_even_with_long_pause():
    repeated = frame(.4, 2, received_at=.2, detections=[])
    records = [frame(0., 1), frame(.2, 2, detections=[]), tick(.2),
               repeated, tick(.4), {**repeated, "at": .6}, tick(.6), tick(.7), tick(1.)]
    result = replay(records, Window("stale", 0., 1., 7), Candidate(.4, .8))
    assert result["first_loss"]["at"] == .7
    assert "stale" in result["first_loss"]["reason"]


def test_recorded_tick_and_availability_uncertainty_can_change_a_borderline_outcome():
    records = [frame(0., 1), frame(.2, 2, detections=[]), tick(.2),
               frame(.54, 3, received_at=.5), tick(.54), tick(.56), tick(.7)]
    on_time = replay(records, Window("boundary", 0., .7, 7), Candidate(.5, .35))
    delayed = [r for r in records if r.get("sequence") != 3]
    delayed.insert(-2, frame(.56, 3, received_at=.5))
    late = replay(delayed, Window("boundary", 0., .7, 7), Candidate(.5, .35))
    assert on_time["pause_recoveries"] == 1 and on_time["first_loss"] is None
    assert late["first_loss"]["at"] == .56
    report = compare(records, [Window("boundary", 0., .7, 7)], availability_uncertainty=.05)
    assert report["availability_uncertainty_s"] == .05
    assert any("Borderline" in note for note in report["limitations"])
    assert on_time["tick_source"] == "recorded_poll"
    assert report["recorded_poll_gap_s"]["max"] == pytest.approx(.34)
    assert on_time["replay_tick_gap_s"]["max"] == pytest.approx(.34)


def test_explicit_second_window_is_separate_and_not_counted_as_uninterrupted_success():
    records = [frame(0., 1), frame(.2, 2, identity=8), tick(.2),
               frame(.4, 3), tick(.4), frame(.6, 4), tick(.6), tick(.8)]
    report = compare(records, [Window("first", 0., .8, 7), Window("second", .4, .8, 7)],
                     confidences=(.5,), pauses=(.35,))
    first, second = report["results"]
    assert first["first_loss"]["at"] == .2
    assert first["duration_s"]["takeover"] == pytest.approx(.6)
    assert second["first_loss"] is None and second["duration_s"]["valid"] == pytest.approx(.4)


def test_scoped_constants_restore_after_normal_use_and_exception():
    original = lifecycle.MIN_CONFIDENCE, lifecycle.DETECTION_PAUSE
    with pytest.raises(RuntimeError):
        with _parameters(.4, .8):
            raise RuntimeError("test failure")
    assert (lifecycle.MIN_CONFIDENCE, lifecycle.DETECTION_PAUSE) == original
    replay([frame(0., 1), tick(.1)], Window("normal", 0., .1, 7), Candidate(.4, .8))
    assert (lifecycle.MIN_CONFIDENCE, lifecycle.DETECTION_PAUSE) == original


def test_default_ticks_check_staleness_between_images_and_takeover_deadline():
    records = [frame(0., 1), {"kind": "state", "at": 3.}]
    result = replay(records, Window("no more images", 0., 3., 7), Candidate(.5, .35))
    assert result["tick_source"] == "generated" and result["tick_count"] == 60
    assert result["first_loss"]["at"] == .5
    assert result["takeover_deadline_reached_at"] == 2.5
    assert sum(result["duration_s"].values()) == 3.


@pytest.mark.parametrize("pause", [False, True])
def test_unprocessed_recorded_tail_is_not_counted_as_validity_or_pause(pause):
    records = [frame(0., 1)]
    if pause:
        records.append(frame(.1, 2, detections=[]))
    records += [tick(.1), {"kind": "state", "at": 1.}]
    result = replay(records, Window("unobserved tail", 0., 1., 7), Candidate(.5, .6))
    assert result["duration_s"]["valid"] == pytest.approx(.1)
    assert result["duration_s"]["paused"] == 0
    assert result["duration_s"]["unevaluated"] == pytest.approx(.9)
    assert sum(result["duration_s"].values()) == pytest.approx(1.)
    assert result["first_loss"] is None  # Neither staleness nor expiry was evaluated.
    assert result["takeover_deadline_reached_at"] is None
    assert all(event["at"] <= .1 for event in result["transitions"])


def test_partial_generated_tick_tail_stays_unevaluated_without_an_extra_tick():
    records = [frame(0., 1), {"kind": "state", "at": .12}]
    result = replay(records, Window("partial tick", 0., .12, 7), Candidate(.5, .6))
    assert result["tick_count"] == 2
    assert result["duration_s"]["valid"] == pytest.approx(.1)
    assert result["duration_s"]["unevaluated"] == pytest.approx(.02)
    assert sum(result["duration_s"].values()) == pytest.approx(.12)


def test_vehicle_rejection_still_latches_and_cli_emits_reproducible_evidence(tmp_path):
    records = [frame(0., 1), tick(.1, vehicle_reason="Vehicle state unavailable"), tick(.2)]
    source, windows, output = (tmp_path / name for name in ("input.jsonl", "windows.json", "output.json"))
    source.write_text("\n".join(json.dumps(r) for r in records))
    windows.write_text(json.dumps([{"name": "vehicle", "start": 0., "end": .2, "target_id": 7}]))
    main([str(source), "--windows", str(windows), "--output", str(output)])
    report = json.loads(output.read_text())
    assert len(report["input_sha256"]) == len(report["helper_sha256"]) == 64
    assert all(r["first_loss"]["reason"] == "Vehicle state unavailable" for r in report["results"])


def test_availability_order_is_not_silently_sorted():
    with pytest.raises(ValueError, match="ordered"):
        replay([frame(.2, 1), tick(.1)], Window("bad", .1, .2, 7), Candidate(.5, .35))


@pytest.mark.parametrize("case", ["short", "higher"])
def test_recorded_dropout_fixtures_reach_recovery_only_with_the_longer_pause(case):
    directory = Path(__file__).resolve().parents[1] / "examples/data/framing_dropout"
    records = [json.loads(line) for line in
               (directory / f"observations-{case}.jsonl").read_text().splitlines()]
    window = Window(**json.loads((directory / f"windows-{case}.json").read_text())[0])
    baseline = replay(records, window, Candidate(.5, .35))
    candidate = replay(records, window, Candidate(.5, .6))
    assert baseline["first_loss"] is not None
    assert "confidence" in baseline["first_loss"]["reason"]
    assert baseline["transitions"][-1]["phase"] == "takeover"
    assert candidate["first_loss"] is None
    assert candidate["pause_recoveries"] == baseline["pause_recoveries"] + 1
    assert candidate["transitions"][-1]["phase"] == "valid"
    assert candidate["transitions"][-1]["at"] == window.end
    assert baseline["duration_s"]["unevaluated"] == candidate["duration_s"]["unevaluated"] == 0
    assert all("jpeg" not in record and record["kind"] != "state" for record in records)


def test_recorded_nested_overlap_waits_for_two_fresh_sole_target_frames():
    directory = Path(__file__).resolve().parents[1] / "examples/data/framing_overlap"
    records = [json.loads(line) for line in (directory / "observations.jsonl").read_text().splitlines()]
    window = Window(**json.loads((directory / "windows.json").read_text())[0])
    result = replay(records, window, Candidate(.5, .6))
    by_sequence = {row["sequence"]: row for row in records if row["kind"] == "frame"}
    assert len(by_sequence[3069]["detections"]) == 2  # No measurement was discarded.
    assert result["engaged"] and result["first_loss"] is None
    assert result["pause_recoveries"] == 1
    pause = next(row for row in result["transitions"] if row["phase"] == "paused")
    recovery = result["transitions"][-1]
    assert pause["at"] == by_sequence[3069]["at"]
    assert recovery["phase"] == "valid" and recovery["at"] == by_sequence[3075]["at"]
    assert by_sequence[3072]["at"] < recovery["at"]  # The first clean image cannot resume.
    assert result["duration_s"]["paused"] == pytest.approx(recovery["at"] - pause["at"])
    assert all(value == 0 for value in pause["axes"].values())
