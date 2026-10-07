"""Offline chronology must preserve production receipt, recovery and admission rules."""
from copy import deepcopy
import hashlib
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


def annotated_frames(*, extra_unknown=False):
    frames = [frame(1, 0.), frame(2, .1, box=[.6, .2, .05, .5]),
              frame(3, .2, box=[.6, .2, .05, .5]), frame(4, .3, box=[.6, .2, .05, .5])]
    frames[1]["detections"].append({"box": [.1, .2, .05, .5], "confidence": .85})
    frames[1]["appearances"].append(None)
    if extra_unknown:
        frames[1]["detections"].append({"box": [.85, .2, .05, .5], "confidence": .8})
        frames[1]["appearances"].append(None)
    for item in frames:
        item["jpeg"] = f"original image {item['sequence']}".encode()
        item["sha256"] = hashlib.sha256(item["jpeg"]).hexdigest()
    return frames


def suppression(frame, index=1, *, reason="reviewed_false_positive"):
    return {"sequence": frame["sequence"], "frame_sha256": frame["sha256"],
            "detection_index": index, "expected_box": list(frame["detections"][index]["box"]),
            "expected_confidence": frame["detections"][index]["confidence"],
            "reason": reason, "evidence_id": "review:source-sequence:exact-detection"}


@pytest.mark.parametrize("mode", ["recorded", "simulated"])
def test_preview_counterfactual_none_and_empty_do_not_change_existing_replay(mode):
    frames = annotated_frames()
    baseline = run(frames, mode=mode)
    assert run(frames, mode=mode, preview_suppressions=None) == baseline
    empty = run(frames, mode=mode, preview_suppressions=[])
    empty["summary"].pop("preview_counterfactual")
    empty["summary"]["limits"] = empty["summary"]["limits"][:-3]
    for decision in empty["frame_decisions"]:
        for key in ("preview_suppression", "preview_detections", "preview_detection_indices",
                    "preview_appearance_available"):
            decision.pop(key, None)
    assert empty == baseline


@pytest.mark.parametrize("mode", ["recorded", "simulated"])
def test_preview_suppression_changes_only_selection_observation_not_association_or_clock(mode):
    frames = annotated_frames()
    before = deepcopy(frames)
    records = [suppression(frames[1])]
    baseline = run(frames, mode=mode, tracker_factory=CurrentAdapter)
    filtered = run(frames, mode=mode, tracker_factory=CurrentAdapter, preview_suppressions=records)
    assert baseline["summary"]["final_preview"]["phase"] == "stopped"
    assert filtered["summary"]["final_preview"]["phase"] == "tracking"
    assert filtered["summary"]["selection"] == baseline["summary"]["selection"]
    assert transitions(filtered, "association") == transitions(baseline, "association")
    assert transitions(filtered, "worker_submitted") == transitions(baseline, "worker_submitted")
    for original, revised in zip(baseline["frame_decisions"], filtered["frame_decisions"]):
        for key in ("sequence", "status", "reason", "detections", "image_age_s", "delivered_at",
                    "submitted_at", "worker_completed_at", "appearance_available"):
            assert original.get(key) == revised.get(key)
        for key in ("native_track_ids", "native_detection_indices", "ephemeral_detections"):
            assert original["association"][key] == revised["association"][key]
    filtered_frame = filtered["frame_decisions"][1]
    assert filtered_frame["preview_detection_indices"] == [0]
    assert filtered_frame["preview_detections"] == filtered_frame["detections"][:1]
    assert filtered_frame["preview_appearance_available"] == [True]
    assert filtered_frame["preview_suppression"]["suppressed_detection_indices"] == [1]
    assert filtered_frame["preview_suppression"]["status"] == "applied"
    assert len(transitions(filtered, "preview_suppression")) == 1
    assert filtered["summary"]["preview_counterfactual"]["suppressed_detections"] == 1
    assert not filtered["summary"]["preview_counterfactual"]["deployable_policy"]
    assert frames == before
    assert records == [suppression(frames[1])]


def test_preview_suppression_keeps_unannotated_unknown_candidates_and_terminal_latch():
    frames = annotated_frames(extra_unknown=True)
    result = run(frames, preview_suppressions=[suppression(frames[1]), suppression(frames[3], 0)])
    second = result["frame_decisions"][1]
    assert second["preview_detection_indices"] == [0, 2]
    assert second["preview_appearance_available"] == [True, False]
    assert second["preview"]["phase"] == "stopped"
    assert "ambiguous" in second["preview"]["detail"].lower()
    assert result["summary"]["final_preview"]["phase"] == "stopped"
    assert result["frame_decisions"][3]["preview_detections"] == []


@pytest.mark.parametrize("mutation,pattern", [
    (lambda record: record.update(sequence=200), "exact source frame"),
    (lambda record: record.update(sequence=True), "exact source frame"),
    (lambda record: record.update(frame_sha256="f" * 64), "hash"),
    (lambda record: record.update(frame_sha256="not-a-hash"), "hash"),
    (lambda record: record.update(detection_index=2), "exact source detection"),
    (lambda record: record.update(detection_index=True), "exact source detection"),
    (lambda record: record.update(expected_box=[.2, .2, .05, .5]), "box/confidence"),
    (lambda record: record.update(expected_confidence=.8), "box/confidence"),
    (lambda record: record.update(reason="unknown"), "reviewed duplicate"),
    (lambda record: record.update(evidence_id="   "), "evidence_id"),
    (lambda record: record.update(evidence_id="x" * 513), "evidence_id"),
    (lambda record: record.update(track_id=1), "exact annotation record fields"),
])
def test_preview_suppression_rejects_unbound_or_unknown_records(mutation, pattern):
    frames = annotated_frames()
    record = suppression(frames[1])
    mutation(record)
    with pytest.raises(ValueError, match=pattern):
        run(frames, preview_suppressions=[record])


def test_preview_suppression_rejects_duplicate_and_preselection_records():
    frames = annotated_frames()
    record = suppression(frames[1])
    with pytest.raises(ValueError, match="duplicate preview suppression"):
        run(frames, preview_suppressions=[record, deepcopy(record)])
    with pytest.raises(ValueError, match="before or at initial selection"):
        run(frames, preview_suppressions=[suppression(frames[0], 0)])
    with pytest.raises(ValueError, match="before or at initial selection"):
        run(frames, at=.1, preview_suppressions=[record])


def test_preview_suppression_checks_jpeg_bytes_when_available():
    frames = annotated_frames()
    record = suppression(frames[1])
    frames[1]["jpeg"] = b"different original image"
    with pytest.raises(ValueError, match="source JPEG"):
        run(frames, preview_suppressions=[record])


def test_preview_suppression_never_repairs_failed_initial_selection():
    frames = annotated_frames()
    frames[0]["detections"] = []
    frames[0]["appearances"] = []
    result = run(frames, preview_suppressions=[suppression(frames[1])])
    assert not result["summary"]["selection"]["accepted"]
    assert result["summary"]["final_preview"]["phase"] == "idle"
    second = result["frame_decisions"][1]
    assert second["preview_detection_indices"] == [0, 1]
    assert second["preview_suppression"]["status"] == "selection_not_accepted"
    assert result["summary"]["preview_counterfactual"]["suppressed_detections"] == 0


def test_preview_suppression_does_not_move_annotations_to_nearby_admitted_frames():
    frames = annotated_frames()
    frames[1]["received_at"] = .04
    frames[1]["available_at"] = .05
    frames[2]["received_at"] = .08
    frames[2]["available_at"] = .09
    result = run(frames, mode="simulated", preview_suppressions=[suppression(frames[1])])
    assert result["frame_decisions"][1]["status"] == "not_analyzed"
    assert result["frame_decisions"][1]["preview_suppression"]["status"] == "not_delivered"
    assert result["frame_decisions"][2]["preview_detection_indices"] == [0]
    assert not transitions(result, "preview_suppression")
    assert result["summary"]["preview_counterfactual"]["suppressed_detections"] == 0


def test_avoiding_immediate_ambiguity_can_still_end_at_the_unchanged_recovery_deadline():
    frames = annotated_frames()
    # The unique surviving candidate's appearance contradicts the selection.
    different = [0., 1.] + [0.] * 206
    for item in frames[1:]:
        item["appearances"][0] = different
    for i in range(4, 34):
        frames.append(frame(i + 1, i * .1, box=[.6, .2, .05, .5]))
        frames[-1]["appearances"][0] = different
    result = run(frames, end=3.4, preview_suppressions=[suppression(frames[1])])
    assert result["frame_decisions"][1]["preview"]["phase"] == "paused"
    assert result["summary"]["final_preview"]["phase"] == "stopped"
    recoveries = transitions(result, "recovery")
    assert any(item["reason"] == "appearance_similarity" for item in recoveries)
    stopped = next(item for item in transitions(result, "preview_transition") if item["phase"] == "stopped")
    assert stopped["at"] == pytest.approx(3.)
    assert stopped["detail"] == "Selected person was lost; select a person again"
    assert not any(item["reason"] == "ambiguous_candidates" for item in recoveries)
