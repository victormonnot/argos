"""Measured-motion recovery must preserve evidence, ambiguity and expiry gates."""
from copy import deepcopy
import json

import pytest

from argos.backends.yaw_stream_source import YawValidator
from argos.console.yaw_preview import YawPreview
from argos.perception.recovery_prototype import make_preview


A = [1.] + [0.] * 207
B = [0., 1.] + [0.] * 206


def detection(track_id=7, center=.2, confidence=.9, *, width=.1, y=.2, height=.4):
    return dict(track_id=track_id, box=[center - width / 2, y, width, height],
                confidence=confidence)


def observation(sequence=1, at=1., *, detections=None, appearances=None, **kwargs):
    return dict(run_id="run", video_id="camera", sequence=sequence, received_at=at,
                width=640, height=480,
                detections=[detection(**kwargs)] if detections is None else detections,
                appearances=[A] if appearances is None else appearances)


def seed(policy="motion", *, preview=None, **kwargs):
    preview = make_preview(policy, **kwargs) if preview is None else preview
    preview.observe(observation(), 1.)
    preview.select(7, revision=preview.revision, now=1.)
    preview.observe(observation(2, 1.2, center=.4, appearances=[None]), 1.2)
    return preview


def gap(preview, sequence=3, at=1.3):
    preview.observe(observation(sequence, at, detections=[], appearances=[]), at)


def test_motion_recovers_two_measured_images_without_moving_the_appearance_reference():
    records = []
    preview = seed(on_recovery=records.append)
    baseline = seed(preview=YawPreview(True, continuous=True))
    for model in (preview, baseline):
        original_reference = deepcopy(model._reference)
        first = observation(3, 1.4, center=.6, track_id=9)
        model.observe(first, 1.4)
        assert model.state(1.4)["phase"] == "paused"
        assert model._reference == original_reference
        assert model._reference_received_at == 1.
        model.observe(observation(4, 1.5, center=.7, track_id=9), 1.5)
    state = preview.state(1.5)
    assert state["phase"] == "tracking" and state["target_id"] == 9
    assert state["selection_id"] == 7
    assert state["error_x"] == pytest.approx(.4)  # Actual measured center.
    assert state["yaw"] == .2
    assert baseline.state(1.5)["phase"] == "paused"
    first, second = records
    assert first["reason"] == "pending_second_image"
    assert second["reason"] == "accepted_appearance"
    for event in records:
        assert event["static_dx"] > .25 and event["dx"] < 1e-12
        assert event["similarity"] == 1. and event["dy"] == 0.
        assert event["width_ratio"] == event["height_ratio"] == 1.
        assert event["motion_anchor_sequence"] == 2
        assert event["motion_anchor_received_at"] == 1.2
        assert event["motion_velocity_x"] == 1.
        assert event["motion_applied"] is True
    assert second["reference_appearance_age_s"] == .5


@pytest.mark.parametrize("failure,reason", [
    ("missing_appearance", "appearance_unavailable"),
    ("different_appearance", "appearance_similarity"),
    ("wide", "width_ratio"), ("tall", "height_ratio"),
    ("vertical", "vertical_distance"), ("horizontal", "horizontal_distance"),
    ("weak", "low_confidence"),
])
def test_motion_does_not_bypass_original_candidate_gates(failure, reason):
    events = []
    preview = seed(on_recovery=events.append)
    for sequence, at, center in ((3, 1.4, .6), (4, 1.5, .7)):
        frame = observation(sequence, at, track_id=9, center=center)
        target = frame["detections"][0]
        if failure == "missing_appearance":
            frame["appearances"] = [None]
        elif failure == "different_appearance":
            frame["appearances"] = [B]
        elif failure == "wide":
            target["box"][0], target["box"][2] = center - .125, .25
        elif failure == "tall":
            target["box"][1], target["box"][3] = .05, .7
        elif failure == "vertical":
            target["box"][1] = .36
        elif failure == "horizontal":
            target["box"][0] = .05
        elif failure == "weak":
            target["confidence"] = .499
        preview.observe(frame, at)
        state = preview.state(at)
        assert state["phase"] == "paused" and state["yaw"] == 0.
        assert state["recovery_deadline_at"] == 4.2
    assert events[-1]["reason"] == reason
    assert len(preview._motion_history) == 2


def test_missing_reference_appearance_cannot_be_created_by_motion():
    events = []
    preview = make_preview("motion", on_recovery=events.append)
    for sequence, at, center in ((1, 1., .2), (2, 1.2, .4)):
        preview.observe(observation(sequence, at, center=center, appearances=[None]), at)
        if sequence == 1:
            preview.select(7, revision=0, now=at)
    preview.observe(observation(3, 1.4, center=.6, track_id=9), 1.4)
    assert preview.state(1.4)["phase"] == "paused"
    assert events[-1]["reason"] == "appearance_unavailable"
    assert events[-1]["reference_appearance_age_s"] is None


def test_new_descriptorless_measurements_do_not_renew_expired_appearance():
    records = []
    preview = make_preview("motion", on_recovery=records.append)
    preview.observe(observation(), 1.)
    preview.select(7, revision=0, now=1.)
    for sequence in range(2, 12):
        at = 1. + (sequence - 1) * .3
        preview.observe(observation(sequence, at, center=.4, appearances=[None]), at)
    assert preview.state(4.)["phase"] == "tracking"
    preview.observe(observation(12, 4.1, center=.4, track_id=9), 4.1)
    assert records[-1]["motion_applied"] is True
    assert records[-1]["reason"] == "reference_appearance_expired"
    assert records[-1]["reference_appearance_age_s"] == pytest.approx(3.1)
    assert preview.state(4.1)["phase"] == "paused"


def test_consumer_confirmation_remains_separate_from_preview_recovery():
    preview = seed()
    validator = YawValidator()
    def demand(at, *, explicit=False):
        state = preview.state(at)
        snapshot = dict(schema_version=1, at=at, run_id="run", environment="real",
            reconnecting=None,
            configuration=dict(environment="real", video_source="device",
                               video_endpoint="/dev/video0"),
            video=dict(source_id="camera", source="device", state="recent",
                       endpoint="/dev/video0", received_at=state["frame_received_at"],
                       rx_age_s=state["frame_age_s"], age_limit_s=1.),
            vision=dict(configured=True, state="recent", frame_age_s=state["frame_age_s"],
                        age_limit_s=1., inference_ms=1.), yaw_preview=state)
        return validator.validate(snapshot, at, at, explicit_selection=explicit)
    assert demand(1.2, explicit=True).valid
    preview.observe(observation(3, 1.4, center=.6, track_id=9), 1.4)
    assert demand(1.4).reason == "target_paused"
    preview.observe(observation(4, 1.5, center=.7, track_id=9), 1.5)
    assert preview.state(1.5)["phase"] == "tracking"
    recovered = demand(1.5)
    assert not recovered.valid and recovered.reason == "target_recovering"
    assert not demand(1.51).valid  # Re-reading cannot count as another image.
    preview.observe(observation(5, 1.6, center=.8, track_id=9), 1.6)
    assert demand(1.6).valid


def test_horizon_uses_source_receipt_and_polls_do_not_confirm_or_borrow_time():
    records = []
    preview = seed(on_recovery=records.append)
    frame = observation(3, 1.4, center=.6, track_id=9)
    original = deepcopy(frame)
    preview.observe(frame, 1.41)
    history = deepcopy(preview._motion_history)
    for now in (1.42, 1.5, 1.6):
        preview.observe(frame, now)
        assert preview.state(now)["phase"] == "paused"
        assert preview._motion_history == history
        metrics = preview._recovery_metrics(preview._frame["detections"][0], now)
        assert metrics["motion_horizon_s"] == pytest.approx(.2)
        assert metrics["motion_predicted_cx"] == pytest.approx(.6)
    assert len(records) == 1 and frame == original


def test_same_receipt_new_sequence_never_adds_motion_or_confirms_new_id():
    preview = seed()
    preview.observe(observation(3, 1.4, center=.6, track_id=9), 1.4)
    preview.observe(observation(4, 1.4, center=.6, track_id=9), 1.41)
    assert preview.state(1.41)["phase"] == "paused"
    assert len(preview._motion_history) == 2
    preview.observe(observation(5, 1.5, center=.7, track_id=9), 1.5)
    assert preview.state(1.5)["phase"] == "tracking"
    before = deepcopy(preview._motion_history)
    preview.observe(observation(6, 1.5, center=.71, track_id=9), 1.51)
    assert preview._motion_history == before


def test_single_measurement_cannot_supply_a_motion_reference():
    records = []
    preview = make_preview("motion", on_recovery=records.append)
    preview.observe(observation(), 1.)
    preview.select(7, revision=0, now=1.)
    preview.observe(observation(2, 1.1, center=.6, track_id=9), 1.1)
    assert records[-1]["motion_applied"] is False
    assert records[-1]["reason"] == "horizontal_distance"


def test_horizon_expiry_and_history_retention_fall_back_to_existing_geometry():
    records = []
    preview = seed(on_recovery=records.append)
    preview.observe(observation(3, 1.65, center=.85, track_id=9, appearances=[None]), 1.65)
    preview.observe(observation(4, 1.71, center=.91, track_id=9), 1.71)
    # Source time prunes the oldest sample at age > .7; one sample cannot predict.
    assert len(preview._motion_history) == 1
    assert records[-1]["motion_applied"] is False
    assert records[-1]["reason"] == "horizontal_distance"
    # Exercise the horizon separately when both selected samples fit the .7s history.
    second = make_preview("motion", on_recovery=records.append)
    second.observe(observation(1, 1., center=.2), 1.)
    second.select(7, revision=0, now=1.)
    second.observe(observation(2, 1.1, center=.3, appearances=[None]), 1.1)
    gap(second, 3, 1.4)
    second.observe(observation(4, 1.61, center=.81, track_id=9), 1.61)
    assert records[-1]["motion_reason"] == "prediction_horizon_expired"
    assert records[-1]["dx"] == records[-1]["static_dx"]


@pytest.mark.parametrize("sign", [-1, 1])
def test_velocity_is_bounded_before_predicting(sign):
    preview = make_preview("motion")
    first, last = (.2, .4) if sign == 1 else (.8, .6)
    preview.observe(observation(1, 1., center=first), 1.)
    preview.select(7, revision=0, now=1.)
    preview.observe(observation(2, 1.01, center=last), 1.01)
    candidate = observation(3, 1.11, center=last + sign * .1, track_id=9)
    preview.observe(candidate, 1.11)
    metrics = preview._recovery_metrics(preview._frame["detections"][0], 1.11)
    assert metrics["motion_velocity_x"] == sign
    assert metrics["motion_predicted_cx"] == pytest.approx(last + sign * .1)


@pytest.mark.parametrize("action", ["clear", "reselect", "stop", "source", "clock"])
def test_motion_history_resets_with_selection_or_source_authority(action):
    preview = seed()
    assert len(preview._motion_history) == 2
    if action == "clear":
        preview.clear()
    elif action == "reselect":
        preview.select(7, revision=preview.revision, now=1.2)
        assert len(preview._motion_history) == 1
        assert preview._motion_history[0]["sequence"] == 2
        return
    elif action == "stop":
        preview.state(1.651)
    elif action == "clock":
        preview.state(1.1)
    else:
        frame = observation(3, 1.3, center=.5)
        frame["video_id"] = "different-camera"
        preview.observe(frame, 1.3)
    assert preview._motion_history == []


@pytest.mark.parametrize("policy", ["motion", "motion_duplicates"])
def test_deadline_and_stale_terminal_latches_never_resume(policy):
    preview = seed(policy)
    for sequence in range(3, 34):
        at = 1.2 + (sequence - 2) * .1
        gap(preview, sequence, at)
    assert preview.state(4.3)["phase"] == "stopped"
    preview.observe(observation(34, 4.4, track_id=7), 4.4)
    assert preview.state(4.4)["phase"] == "stopped"
    assert preview._motion_history == []
    preview.select(7, revision=preview.revision, now=4.4)
    assert preview.state(4.851)["phase"] == "stopped"
    assert preview._motion_history == []


def pair_frame(sequence, at, *, ids=(9, 10), scores=(.9, .8), appearances=None):
    return observation(sequence, at,
        detections=[detection(ids[0], .6, scores[0]), detection(ids[1], .601, scores[1])],
        appearances=[A, A] if appearances is None else appearances)


def test_strict_duplicates_preserve_pending_id_when_confidence_order_changes():
    events = []
    preview = seed("motion_duplicates", on_policy=events.append)
    first = pair_frame(3, 1.4)
    before = deepcopy(first)
    preview.observe(first, 1.4)
    assert preview.state(1.4)["phase"] == "paused"
    assert preview.preview_detection_indices == [0]
    second = pair_frame(4, 1.5, scores=(.7, .99))
    preview.observe(second, 1.5)
    assert preview.state(1.5)["target_id"] == 9
    assert preview.state(1.5)["phase"] == "tracking"
    assert preview.preview_detection_indices == [0]
    assert events[-1]["removed_detection_indices"] == [1]
    assert first == before
    public = preview.preview_detections
    assert "appearance" not in public[0]
    public[0]["box"][0] = 0
    assert preview.preview_detections[0]["box"][0] != 0


def test_paused_selected_id_has_priority_over_a_higher_score_duplicate():
    preview = seed("motion_duplicates")
    gap(preview)
    frame = pair_frame(4, 1.4, ids=(9, 7), scores=(.99, .6))
    preview.observe(frame, 1.4)
    assert preview.preview_detection_indices == [1]
    assert preview.state(1.4)["phase"] == "tracking"
    assert preview.state(1.4)["target_id"] == 7


def test_active_selected_id_does_not_trigger_duplicate_filtering():
    preview = seed("motion_duplicates")
    preview.observe(pair_frame(3, 1.4, ids=(9, 7), scores=(.99, .6)), 1.4)
    assert preview.preview_detection_indices == [0, 1]
    assert preview.state(1.4)["target_id"] == 7


@pytest.mark.parametrize("control,reason", [
    ("different_appearance", "different_appearance"),
    ("missing_appearance", "appearance_unavailable"),
    ("distinct_geometry", "separate_geometry"),
    ("nonclique", "separate_geometry"),
])
def test_distinct_or_unknown_candidates_preserve_ambiguity_stop(control, reason):
    events = []
    preview = seed("motion_duplicates", on_policy=events.append)
    frame = pair_frame(3, 1.4)
    if control == "different_appearance":
        frame["appearances"][1] = B
    elif control == "missing_appearance":
        frame["appearances"][1] = None
    elif control == "distinct_geometry":
        frame["detections"][1]["box"][0] = .75
    else:
        frame["detections"] = [detection(i + 9, .6 + i * .006) for i in range(3)]
        frame["appearances"] = [A, A, A]
    preview.observe(frame, 1.4)
    assert preview.state(1.4)["phase"] == "stopped"
    assert events[-1]["duplicate_reason"] == reason
    assert events[-1]["removed_detection_indices"] == []


def test_duplicate_rule_never_removes_weak_unknown_candidates():
    preview = seed("motion_duplicates")
    frame = pair_frame(3, 1.4)
    frame["detections"].append(detection(11, .8, .4))
    frame["appearances"].append(None)
    preview.observe(frame, 1.4)
    assert preview.preview_detection_indices == [0, 2]
    assert preview.state(1.4)["phase"] == "paused"


def test_equal_confidence_prefers_lowest_source_index_not_track_number():
    preview = seed("motion_duplicates")
    preview.observe(pair_frame(3, 1.4, ids=(100, 9), scores=(.8, .8)), 1.4)
    assert preview.preview_detection_indices == [0]
    assert preview._recovery_candidate[0] == 100


def test_duplicate_group_and_events_are_immutable_across_repeated_frame_reads():
    events = []
    preview = seed("motion_duplicates", on_policy=events.append)
    frame = pair_frame(3, 1.4)
    preview.observe(frame, 1.4)
    frozen = deepcopy(events)
    for now in (1.41, 1.5, 1.6):
        preview.observe(frame, now)
        assert preview.state(now)["phase"] == "paused"
        assert preview.preview_detection_indices == [0]
    assert events == frozen
    # The complete raw observation is still checked: removed inputs cannot mutate.
    changed = deepcopy(frame)
    changed["detections"][1]["confidence"] = .81
    preview.observe(changed, 1.61)
    assert preview.state(1.61)["phase"] == "stopped"


def test_motion_without_duplicate_ablation_retains_original_ambiguity_latch():
    preview = seed("motion")
    preview.observe(pair_frame(3, 1.4), 1.4)
    assert preview.state(1.4)["phase"] == "stopped"
    assert preview.preview_detection_indices == [0, 1]


@pytest.mark.parametrize("clear_first", [False, True])
def test_explicit_reselection_can_choose_a_previously_suppressed_candidate(clear_first):
    preview = seed("motion_duplicates")
    frame = pair_frame(3, 1.4)
    preview.observe(frame, 1.4)
    assert preview.preview_detection_indices == [0]
    if clear_first:
        preview.clear()
    state = preview.select(10, revision=preview.revision, now=1.4)
    assert state["phase"] == "tracking" and state["target_id"] == 10
    assert preview.preview_detection_indices == [0, 1]
    assert len(preview._motion_history) == 1
    assert preview._motion_history[0]["sequence"] == 3
    assert preview._recovery_candidate is None
    preview.observe(frame, 1.41)
    assert preview.state(1.41)["target_id"] == 10


def test_diagnostic_callbacks_are_scalar_only_and_cannot_change_authority():
    events = []
    def callback(event):
        events.append(json.loads(json.dumps(event)))
        raise RuntimeError("diagnostic sink is unavailable")
    preview = seed("motion_duplicates", on_recovery=callback, on_policy=callback)
    preview.observe(pair_frame(3, 1.4), 1.4)
    preview.observe(pair_frame(4, 1.5), 1.5)
    assert preview.state(1.5)["phase"] == "tracking"
    assert all("appearances" not in event for event in events)
    assert preview.metadata["offline_only"] is True


@pytest.mark.parametrize("value", [None, "baseline", "unfrozen", False])
def test_unrecognized_policy_is_rejected(value):
    with pytest.raises(ValueError, match="unknown offline"):
        make_preview(value)


def test_noncallable_policy_diagnostics_are_rejected():
    with pytest.raises(ValueError, match="callable"):
        make_preview("motion", on_policy=[])
