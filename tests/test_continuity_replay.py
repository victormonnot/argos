"""Offline chronology must preserve production receipt, recovery and admission rules."""
from copy import deepcopy
import json

import pytest

from argos.perception.continuity_replay import replay
from argos.perception.image_tracks import ImageTracker


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


class CurrentAdapter:
    metadata = {"name": "current-test-adapter"}

    def __init__(self, *, on_diagnostic):
        self.tracker = ImageTracker(on_diagnostic=on_diagnostic)

    def reset(self):
        self.tracker.reset()

    def update(self, detections, captured_at, *, appearances, image, width, height):
        self.last_detection_indices = list(range(len(detections)))
        return self.tracker.update(detections, captured_at, appearances=appearances)


@pytest.mark.parametrize("mode", ["recorded", "simulated"])
def test_current_comparison_adapter_preserves_entire_default_replay(mode):
    frames = [frame(1, 0.), frame(2, .1, box=[.6, .2, .05, .5]),
              frame(3, .2, box=[.6, .2, .05, .5]), frame(4, .3)]
    baseline = run(frames, mode=mode)
    comparison = run(frames, mode=mode, tracker_factory=CurrentAdapter)
    assert comparison["summary"].pop("tracker") == CurrentAdapter.metadata
    costs = comparison["summary"].pop("association_cost")
    assert costs["tracker_wall_ms"]["count"] == 4
    assert costs["tracker_cpu_ms"]["p50"] >= 0
    comparison["summary"]["limits"] = comparison["summary"]["limits"][:-3]
    for decision in comparison["frame_decisions"]:
        if decision["status"] == "accepted":
            details = decision.pop("association")
            assert details["ephemeral_detections"] == []
    assert comparison == baseline


class PartialAdapter:
    """Native tracker hides box zero, returns box one with a predicted position."""

    def __init__(self, *, on_diagnostic):
        pass

    def reset(self):
        pass

    def update(self, detections, captured_at, *, appearances, image, width, height):
        assert (width, height) == (640, 480)
        assert image == (captured_at, "pixels")
        self.last_detection_indices = [1]
        self.last_unassigned = [{"detection_index": 0, "reason": "unconfirmed"}]
        self.last_output_confirmed = [True]
        return [{"track_id": 7, "box": [.1, .1, .1, .1], "confidence": .1}]


@pytest.mark.parametrize("mode", ["recorded", "simulated"])
def test_partial_adapter_keeps_all_measured_boxes_appearances_and_ambiguity(mode):
    frames = [frame(1, 0.), frame(2, .1)]
    for item in frames:
        item["detections"].append({"box": [.8, .2, .05, .5], "confidence": .85})
        item["appearances"] = [None, APPEARANCE]
    before = deepcopy(frames)
    result = run(frames, mode=mode, tracker_factory=PartialAdapter,
                 image_provider=lambda item: (item["received_at"], "pixels"))
    assert result["summary"]["selection"]["accepted"]
    decisions = result["frame_decisions"]
    for original, decision in zip(frames, decisions):
        assert decision["status"] == "accepted"
        measured = [{key: detection[key] for key in ("box", "confidence")}
                    for detection in decision["detections"]]
        assert measured == original["detections"]
        assert decision["appearance_available"] == [False, True]
        assert decision["detections"][1]["track_id"] == 7
        assert decision["association"]["native_detection_indices"] == [1]
        assert decision["association"]["native_output_confirmed"] == [True]
        assert decision["association"]["unassigned"][0]["reason"] == "unconfirmed"
    assert decisions[0]["detections"][0]["track_id"] != decisions[1]["detections"][0]["track_id"]
    assert decisions[1]["preview"]["phase"] == "stopped"
    assert "ambiguous" in decisions[1]["preview"]["detail"].lower()
    assert frames == before


def test_comparison_adapter_reordered_assignments_restore_source_alignment():
    class Reversed(CurrentAdapter):
        def update(self, detections, captured_at, **kwargs):
            self.last_detection_indices = [1, 0]
            return [{"track_id": 70}, {"track_id": 80}]

    first = frame(1, 0.)
    first["detections"].append({"box": [.8, .2, .05, .5], "confidence": .8})
    first["appearances"].append(None)
    result = run([first], tracker_factory=Reversed)
    decision = result["frame_decisions"][0]
    assert [item["track_id"] for item in decision["detections"]] == [80, 70]
    assert decision["appearance_available"] == [True, False]
    assert result["summary"]["selection"]["track_id"] == 80


@pytest.mark.parametrize("indices,outputs,pattern", [
    ([0, 0], [{"track_id": 1}, {"track_id": 2}], "map uniquely"),
    ([1], [{"track_id": 1}], "map uniquely"),
    ([0], [], "map uniquely"),
    ([0], [{"track_id": 2**52}], "unique positive"),
])
def test_comparison_adapter_rejects_invalid_native_mapping(indices, outputs, pattern):
    class Invalid(CurrentAdapter):
        def update(self, detections, captured_at, **kwargs):
            self.last_detection_indices = indices
            return outputs

    with pytest.raises(RuntimeError, match=pattern):
        run([frame(1, 0.)], tracker_factory=Invalid)


def test_comparison_rejects_image_provider_without_an_adapter():
    with pytest.raises(ValueError, match="requires a custom tracker"):
        run([frame(1, 0.)], image_provider=lambda item: None)


@pytest.mark.parametrize("mode", ["recorded", "simulated"])
@pytest.mark.parametrize("failed_component", ["tracker", "image_provider"])
def test_comparison_adapter_failures_abort_instead_of_becoming_target_losses(mode, failed_component):
    class Broken(CurrentAdapter):
        def update(self, *args, **kwargs):
            raise ValueError("broken association")

    def broken_pixels(frame):
        raise TypeError("broken pixels")

    with pytest.raises(RuntimeError, match="offline comparison tracker failed at frame 1") as caught:
        run([frame(1, 0.)], mode=mode,
            tracker_factory=Broken if failed_component == "tracker" else CurrentAdapter,
            image_provider=broken_pixels if failed_component == "image_provider" else None)
    assert isinstance(caught.value.__cause__, (ValueError, TypeError))
