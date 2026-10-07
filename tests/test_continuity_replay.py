"""Offline chronology must preserve production receipt, recovery and admission rules."""
from copy import deepcopy
import json

import pytest

from argos.perception.continuity_replay import replay


APPEARANCE = [1.] + [0.] * 207
BOX = [.4, .2, .05, .5]


def frame(sequence, received, *, available=None, box=None, confidence=.9,
          appearances=True, empty=False, worker_ms=10.):
    return {"sequence": sequence, "received_at": received,
            "available_at": received + .01 if available is None else available,
            "width": 640, "height": 480, "worker_ms": worker_ms, "inference_ms": 7.,
            "detections": [] if empty else [{"box": list(BOX if box is None else box), "confidence": confidence}],
            "appearances": [] if empty else [list(APPEARANCE) if appearances else None]}


def run(frames, *, end=.5, at=.02, mode="recorded", **kwargs):
    return replay(frames, start=0., end=end, selection={"at": at, "box": list(BOX)}, mode=mode, **kwargs)


def transitions(result, kind):
    return [event for event in result["events"] if event["kind"] == kind]


def test_completion_is_delivered_before_previous_frame_expiry_at_same_time():
    result = run([frame(1, 0.), frame(2, .4, available=.46)], end=.6)
    assert result["summary"]["selection"]["accepted"]
    assert result["summary"]["final_preview"]["phase"] == "tracking"
    assert not any(event["phase"] == "stopped" for event in transitions(result, "preview_transition"))
    assert result["frame_decisions"][1]["image_age_s"] == pytest.approx(.06)


def test_intermediate_ticks_latch_staleness_before_later_result():
    result = run([frame(1, 0.), frame(2, .5, available=.6)], end=.8)
    stopped = next(event for event in transitions(result, "preview_transition") if event["phase"] == "stopped")
    assert stopped["at"] == pytest.approx(.46)
    assert stopped["detail"] == "Selected target image is stale"
    assert result["summary"]["final_preview"]["phase"] == "stopped"
    assert result["summary"]["final_consumer"]["reason"] == "selection_required"


def test_new_id_recovery_and_consumer_each_require_distinct_images():
    moved = [.6, .2, .05, .5]
    result = run([frame(1, 0.), frame(2, .1, box=moved), frame(3, .2, box=moved),
                  frame(4, .3, box=moved)], end=.4)
    decisions = result["frame_decisions"]
    assert [item["preview"]["phase"] for item in decisions] == ["idle", "paused", "tracking", "tracking"]
    assert [item["consumer"]["valid"] for item in decisions] == [False, False, False, True]
    assert decisions[2]["consumer"]["reason"] == "target_recovering"
    recoveries = transitions(result, "recovery")
    assert [item["reason"] for item in recoveries[:2]] == ["pending_second_image", "accepted_appearance"]
    valid_again = [event for event in transitions(result, "consumer_transition")
                   if event["valid"] and event["at"] > .03]
    assert len(valid_again) == 1 and valid_again[0]["at"] == pytest.approx(.31)
    assert any(event["kind"] == "association" for event in result["events"])


def test_one_failed_controlled_selection_is_never_retried():
    result = run([frame(1, 0., empty=True), frame(2, .1)], end=.3)
    assert not result["summary"]["selection"]["accepted"]
    assert result["summary"]["final_preview"]["phase"] == "idle"
    assert len(transitions(result, "selection")) == 1


def test_selection_refuses_multiple_matching_boxes():
    first = frame(1, 0.)
    first["detections"].append({"box": [.401, .2, .05, .5], "confidence": .9})
    first["appearances"].append(APPEARANCE)
    result = run([first], end=.2)
    assert not result["summary"]["selection"]["accepted"]


def test_simulated_real_scheduler_uses_latest_frame_and_one_worker():
    frames = [frame(i + 1, i * .02, worker_ms=80.) for i in range(6)]
    result = run(frames, at=.09, end=.3, mode="simulated")
    submissions = transitions(result, "worker_submitted")
    assert [(item["sequence"], item["at"]) for item in submissions] == [(1, 0.), (6, .1)]
    assert [item["sequence"] for item in result["frame_decisions"] if item["status"] == "accepted"] == [1, 6]
    assert result["summary"]["frame_status_counts"]["not_analyzed"] == 4
    assert result["summary"]["selection"]["accepted"]


def test_simulated_stale_worker_result_is_rejected_before_tracking():
    frames = [frame(i + 1, i * .1, worker_ms=1100.) for i in range(14)]
    result = run(frames, at=.02, end=1.35, mode="simulated")
    assert result["frame_decisions"][0]["status"] == "rejected"
    assert result["summary"]["accepted_frames"] == 0
    assert not transitions(result, "association")
    assert len(transitions(result, "result_rejected")) == 1
    assert transitions(result, "worker_submitted")[1]["sequence"] == 12


def test_declared_worker_stress_can_expire_a_selected_target():
    frames = [frame(i + 1, i * .1, worker_ms=10.) for i in range(13)]
    ordinary = run(frames, at=.02, end=1.3, mode="simulated")
    stressed = run(frames, at=.02, end=1.3, mode="simulated", extra_worker_ms=500.)
    assert ordinary["summary"]["selection"]["accepted"]
    assert not stressed["summary"]["selection"]["accepted"]
    assert stressed["summary"]["extra_worker_ms"] == 500
    assert stressed["frame_decisions"][0]["image_age_s"] >= .5


def test_source_inputs_are_unchanged_and_output_contains_no_descriptors():
    frames = [frame(1, 0.), frame(2, .1)]
    before = deepcopy(frames)
    result = run(frames, end=.3)
    assert frames == before
    encoded = json.dumps(result, allow_nan=False)
    assert '"appearances"' not in encoded
    assert '"historical_parity": false' in encoded
    assert result["summary"]["identity_status"] == "human_unvalidated"
    assert sum(result["summary"]["phase_seconds"].values()) == pytest.approx(.3)


@pytest.mark.parametrize("mutation,pattern", [
    (lambda frames: frames[1].update(received_at=0.), "strictly increase"),
    (lambda frames: frames[1].update(sequence=1), "strictly increase"),
    (lambda frames: frames[1].update(available_at=.05), "precede"),
    (lambda frames: frames[1].update(width=800), "consistent"),
    (lambda frames: frames[1].update(worker_ms=float("nan")), "finite"),
    (lambda frames: frames[1]["detections"][0].update(track_id=99), "invalid detection"),
    (lambda frames: frames[1].update(appearances=[]), "align"),
])
def test_invalid_or_pretracked_input_is_rejected(mutation, pattern):
    frames = [frame(1, 0.), frame(2, .1)]
    mutation(frames)
    with pytest.raises(ValueError, match=pattern):
        run(frames)


def test_recorded_mode_does_not_invent_an_extra_worker_time():
    with pytest.raises(ValueError, match="simulated-worker"):
        run([frame(1, 0.)], extra_worker_ms=1)
