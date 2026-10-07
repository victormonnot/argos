"""Holding a selected velocity must not manufacture identity or image evidence."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

import pytest

from argos.backends.yaw_stream_source import YawValidator
from argos.perception.recovery_prototype import make_preview


A = [1.] + [0.] * 207
B = [0., 1.] + [0.] * 206
POLICIES = ("motion_held", "motion_held_duplicates")


def detection(track_id=7, center=.2, confidence=.9, *, width=.1, y=.2, height=.4):
    return dict(track_id=track_id, box=[center - width / 2, y, width, height],
                confidence=confidence)


def observation(sequence=1, at=1., *, detections=None, appearances=None, **kwargs):
    return dict(run_id="run", video_id="camera", sequence=sequence, received_at=at,
                width=640, height=480,
                detections=[detection(**kwargs)] if detections is None else detections,
                appearances=[A] if appearances is None else appearances)


def seed(policy="motion_held", **kwargs):
    preview = make_preview(policy, **kwargs)
    preview.observe(observation(), 1.)
    preview.select(7, revision=preview.revision, now=1.)
    # The old appearance stays paired with seq 1; the selected motion anchor is
    # seq 2. At candidate time the former sample ages out of the rolling history.
    preview.observe(observation(2, 1.6, center=.5, appearances=[None]), 1.6)
    return preview


@pytest.mark.parametrize("policy", POLICIES)
def test_held_velocity_survives_pruned_history_and_confirms_two_actual_images(policy):
    events = []
    preview = seed(policy, on_recovery=events.append)
    old_policy = seed("motion")
    reference = deepcopy(preview._reference)
    anchor = preview._held_motion
    for model in (preview, old_policy):
        model.observe(observation(3, 1.8, center=.6, track_id=9), 1.8)
        assert model.state(1.8)["phase"] == "paused"
        assert len(model._motion_history) == 1
    assert preview._held_motion is anchor
    assert preview._reference == reference and preview._reference_received_at == 1.
    assert events[-1]["reason"] == "pending_second_image"
    for model in (preview, old_policy):
        model.observe(observation(4, 1.9, center=.65, track_id=9), 1.9)
    state = preview.state(1.9)
    assert state["phase"] == "tracking" and state["target_id"] == 9
    assert state["selection_id"] == 7 and state["error_x"] == pytest.approx(.3)
    assert old_policy.state(1.9)["phase"] == "paused"
    assert events[-1]["reason"] == "accepted_appearance"
    for event, horizon in zip(events, (.2, .3)):
        assert event["motion_anchor_sequence"] == 2
        assert event["motion_previous_sequence"] == 1
        assert event["motion_previous_received_at"] == 1.
        assert event["motion_anchor_received_at"] == 1.6
        assert event["motion_anchor_cx"] == .5
        assert event["motion_velocity_x"] == pytest.approx(.5)
        assert event["motion_horizon_s"] == pytest.approx(horizon)
        assert event["motion_residual_x"] == pytest.approx(0.)
        assert event["motion_uncertainty_margin_x"] == pytest.approx(.1 * horizon)
        assert event["dx"] == event["motion_effective_dx"]
        assert event["motion_applied"] is True and event["static_dx"] > .25


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("control,reason", [
    ("wrong_appearance", "appearance_similarity"),
    ("missing_appearance", "appearance_unavailable"),
    ("weak", "low_confidence"), ("wide", "width_ratio"),
    ("tall", "height_ratio"), ("vertical", "vertical_distance"),
])
def test_rejected_or_pending_candidates_never_refresh_held_evidence(policy, control, reason):
    events = []
    preview = seed(policy, on_recovery=events.append)
    anchor, last_selected = preview._held_motion, preview._held_last_selected
    for seq, at, center in ((3, 1.8, .6), (4, 1.9, .65)):
        frame = observation(seq, at, center=center, track_id=9)
        target = frame["detections"][0]
        if control == "wrong_appearance":
            frame["appearances"] = [B]
        elif control == "missing_appearance":
            frame["appearances"] = [None]
        elif control == "weak":
            target["confidence"] = .499
        elif control == "wide":
            target["box"][0], target["box"][2] = center - .125, .25
        elif control == "tall":
            target["box"][1], target["box"][3] = .05, .7
        else:
            target["box"][1] = .36
        preview.observe(frame, at)
        assert preview.state(at)["phase"] == "paused"
        assert preview.state(at)["yaw"] == 0.
        assert preview._held_motion is anchor
        assert preview._held_last_selected is last_selected
    assert events[-1]["reason"] == reason
    assert preview.state(1.9)["recovery_deadline_at"] == 4.6


def test_repeated_polls_and_equal_receipts_never_renew_or_confirm():
    events = []
    preview = seed(on_recovery=events.append)
    anchor = preview._held_motion
    preview.observe(observation(3, 1.6, center=.51), 1.61)
    assert preview._held_motion is anchor
    assert preview._held_last_selected.sequence == 2
    frame = observation(4, 1.8, center=.6, track_id=9)
    before = deepcopy(frame)
    preview.observe(frame, 1.81)
    for now in (1.82, 1.9, 2.0):
        preview.observe(frame, now)
        assert preview.state(now)["phase"] == "paused"
        assert preview._held_motion is anchor
        metrics = preview._recovery_metrics(preview._frame["detections"][0], now)
        assert metrics["motion_horizon_s"] == pytest.approx(.2)
        assert metrics["motion_uncertainty_margin_x"] == pytest.approx(.02)
    assert len(events) == 1 and frame == before
    preview.observe(observation(5, 1.8, center=.6, track_id=9), 2.01)
    assert preview.state(2.01)["phase"] == "paused"
    assert preview._held_motion is anchor
    preview.observe(observation(6, 1.9, center=.65, track_id=9), 2.02)
    assert preview.state(2.02)["phase"] == "tracking"
    assert preview._held_motion.anchor.sequence == 6
    assert preview._held_motion.anchor.received_at == 1.9


@pytest.mark.parametrize("at,applied,reason", [
    (2.1, True, "pending_second_image"),
    (2.10001, False, "horizontal_distance"),
])
def test_horizon_expiration_is_based_on_source_receipt_and_falls_back(at, applied, reason):
    events = []
    preview = seed(on_recovery=events.append)
    preview.observe(observation(3, at, center=.5 + .5 * (at - 1.6), track_id=9), at)
    assert events[-1]["motion_applied"] is applied
    assert events[-1]["reason"] == reason
    if not applied:
        assert events[-1]["motion_reason"] == "prediction_horizon_expired"
        assert events[-1]["dx"] == events[-1]["static_dx"]
        assert events[-1]["motion_residual_x"] is None
        assert events[-1]["motion_uncertainty_margin_x"] is None


@pytest.mark.parametrize("residual,reason", [(.21, "pending_second_image"),
                                            (.22, "horizontal_distance")])
def test_fixed_uncertainty_margin_consumes_existing_geometry_allowance(residual, reason):
    events = []
    preview = make_preview("motion_held", on_recovery=events.append)
    preview.observe(observation(1, 1., center=.4), 1.)
    preview.select(7, revision=preview.revision, now=1.)
    preview.observe(observation(2, 1.5, center=.4), 1.5)
    preview.observe(observation(3, 1.9, center=.4 + residual, track_id=9), 1.9)
    event = events[-1]
    assert event["motion_residual_x"] == pytest.approx(residual)
    assert event["motion_uncertainty_margin_x"] == pytest.approx(.04)
    assert event["dx"] == pytest.approx(residual + .04)
    assert event["reason"] == reason
    assert preview.metadata["prediction_uncertainty_kind"] == "fixed_assumption_not_calibrated"


@pytest.mark.parametrize("sign", [-1, 1])
def test_held_velocity_cap_and_command_use_current_measurement(sign):
    events = []
    preview = make_preview("motion_held", on_recovery=events.append)
    first, anchor = (.2, .4) if sign == 1 else (.8, .6)
    preview.observe(observation(1, 1., center=first), 1.)
    preview.select(7, revision=preview.revision, now=1.)
    preview.observe(observation(2, 1.01, center=anchor), 1.01)
    for seq, at in ((3, 1.11), (4, 1.21)):
        # A measured displacement from the prediction must remain in commands.
        center = anchor + sign * (at - 1.01) + .03
        preview.observe(observation(seq, at, center=center, track_id=9), at)
    assert all(event["motion_velocity_x"] == sign for event in events)
    state = preview.state(1.21)
    assert state["phase"] == "tracking"
    assert state["error_x"] == pytest.approx(2 * (center - .5))


def test_absent_or_old_selected_pair_falls_back_to_original_static_geometry():
    events = []
    preview = make_preview("motion_held", on_recovery=events.append)
    preview.observe(observation(), 1.)
    preview.select(7, revision=preview.revision, now=1.)
    preview.observe(observation(2, 1.8, center=.5, appearances=[None]), 1.8)
    assert preview._held_motion is None
    preview.observe(observation(3, 1.9, center=.6, track_id=9), 1.9)
    assert events[-1]["motion_reason"] == "insufficient_selected_history"
    assert events[-1]["dx"] == events[-1]["static_dx"]
    assert events[-1]["reason"] == "horizontal_distance"


def test_descriptorless_selected_images_cannot_renew_expired_appearance_reference():
    events = []
    preview = make_preview("motion_held", on_recovery=events.append)
    preview.observe(observation(), 1.)
    preview.select(7, revision=preview.revision, now=1.)
    for seq in range(2, 12):
        at = 1. + (seq - 1) * .3
        preview.observe(observation(seq, at, center=.4, appearances=[None]), at)
    preview.observe(observation(12, 4.1, center=.4, track_id=9), 4.1)
    assert events[-1]["motion_applied"] is True
    assert events[-1]["reference_appearance_age_s"] == pytest.approx(3.1)
    assert events[-1]["reason"] == "reference_appearance_expired"
    assert preview.state(4.1)["phase"] == "paused"


def test_expired_motion_still_allows_original_static_recovery():
    events = []
    preview = seed(on_recovery=events.append)
    for seq, at in ((3, 2.11), (4, 2.21)):
        preview.observe(observation(seq, at, center=.21, track_id=9), at)
    assert preview.state(2.21)["phase"] == "tracking"
    assert [event["reason"] for event in events] == ["pending_second_image", "accepted_appearance"]
    assert all(event["motion_reason"] == "prediction_horizon_expired" for event in events)
    assert all(event["dx"] == event["static_dx"] for event in events)


def test_motion_does_not_invent_a_missing_appearance_reference():
    events = []
    preview = make_preview("motion_held", on_recovery=events.append)
    preview.observe(observation(1, 1., appearances=[None]), 1.)
    preview.select(7, revision=preview.revision, now=1.)
    preview.observe(observation(2, 1.6, center=.5, appearances=[None]), 1.6)
    preview.observe(observation(3, 1.8, center=.6, track_id=9), 1.8)
    assert events[-1]["motion_applied"] is True
    assert events[-1]["reason"] == "appearance_unavailable"
    assert events[-1]["reference_appearance_age_s"] is None
    assert preview.state(1.8)["phase"] == "paused"


@pytest.mark.parametrize("action", ["clear", "reselect", "source", "stop", "clock"])
def test_selection_source_or_terminal_change_invalidates_held_estimate(action):
    preview = seed()
    assert preview._held_motion is not None
    if action == "clear":
        preview.clear()
    elif action == "reselect":
        preview.select(7, revision=preview.revision, now=1.6)
        assert preview._held_last_selected.sequence == 2
        assert preview._held_motion is None
        return
    elif action == "source":
        frame = observation(3, 1.7, center=.55)
        frame["video_id"] = "other-camera"
        preview.observe(frame, 1.7)
    elif action == "stop":
        preview.state(2.051)
    else:
        preview.state(1.5)
    assert preview._held_motion is None and preview._held_last_selected is None


@pytest.mark.parametrize("policy", POLICIES)
def test_deadline_and_stale_terminal_latches_do_not_resume_from_future_candidates(policy):
    preview = seed(policy)
    for seq in range(3, 35):
        at = 1.6 + (seq - 2) * .1
        preview.observe(observation(seq, at, detections=[], appearances=[]), at)
    assert preview.state(4.8)["phase"] == "stopped"
    assert preview._held_motion is None
    preview.observe(observation(35, 4.9, center=.5, track_id=7), 4.9)
    assert preview.state(4.9)["phase"] == "stopped"
    preview.select(7, revision=preview.revision, now=4.9)
    preview.state(5.351)
    preview.observe(observation(36, 5.4, center=.5, track_id=7), 5.4)
    assert preview.state(5.4)["phase"] == "stopped"
    assert preview._held_motion is None


def pair(sequence, at, *, ids=(9, 10), scores=(.9, .8), appearances=None):
    return observation(sequence, at,
        detections=[detection(ids[0], .6, scores[0]), detection(ids[1], .601, scores[1])],
        appearances=[A, A] if appearances is None else appearances)


@pytest.mark.parametrize("control,reason", [
    ("different_appearance", "different_appearance"),
    ("unknown_appearance", "appearance_unavailable"),
    ("different_location", "separate_geometry"),
    ("nonclique", "separate_geometry"),
])
def test_duplicate_variant_keeps_competing_or_unknown_people_and_latches(control, reason):
    events = []
    preview = seed("motion_held_duplicates", on_policy=events.append)
    frame = pair(3, 1.8)
    if control == "different_appearance":
        frame["appearances"][1] = B
    elif control == "unknown_appearance":
        frame["appearances"][1] = None
    elif control == "different_location":
        frame["detections"][1]["box"][0] = .75
    else:
        frame["detections"] = [detection(i + 9, .6 + i * .006) for i in range(3)]
        frame["appearances"] = [A, A, A]
    preview.observe(frame, 1.8)
    assert events[-1]["duplicate_reason"] == reason
    assert events[-1]["removed_detection_indices"] == []
    assert preview.state(1.8)["phase"] == "stopped"
    assert preview._held_motion is None
    preview.observe(observation(4, 1.9, center=.65, track_id=7), 1.9)
    assert preview.state(1.9)["phase"] == "stopped"


def test_duplicate_policy_preserves_pending_then_selected_identity_before_confidence():
    preview = seed("motion_held_duplicates")
    first = pair(3, 1.8)
    preview.observe(first, 1.8)
    assert preview.preview_detection_indices == [0]
    assert preview.state(1.8)["phase"] == "paused"
    preview.observe(pair(4, 1.9, scores=(.6, .99)), 1.9)
    assert preview.state(1.9)["target_id"] == 9
    assert preview.state(1.9)["phase"] == "tracking"
    preview.observe(observation(5, 2., detections=[], appearances=[]), 2.)
    preview.observe(pair(6, 2.1, ids=(10, 9), scores=(.99, .6)), 2.1)
    assert preview.preview_detection_indices == [1]
    assert preview.state(2.1)["target_id"] == 9
    assert preview.state(2.1)["phase"] == "tracking"


def test_held_only_policy_never_filters_ambiguity():
    preview = seed()
    preview.observe(pair(3, 1.8), 1.8)
    assert preview.preview_detection_indices == [0, 1]
    assert preview.state(1.8)["phase"] == "stopped"


def test_explicit_reselection_resets_held_evidence_and_can_choose_removed_box():
    preview = seed("motion_held_duplicates")
    preview.observe(pair(3, 1.8), 1.8)
    assert preview.preview_detection_indices == [0]
    preview.select(10, revision=preview.revision, now=1.8)
    assert preview.state(1.8)["target_id"] == 10
    assert preview.preview_detection_indices == [0, 1]
    assert preview._held_motion is None
    assert preview._held_last_selected.sequence == 3


@pytest.mark.parametrize("policy", POLICIES)
def test_dry_consumer_still_requires_a_further_distinct_image_after_preview_recovery(policy):
    preview = seed(policy)
    validator = YawValidator()

    def demand(at, explicit=False):
        state = preview.state(at)
        snapshot = dict(schema_version=1, at=at, run_id="run", environment="real",
            reconnecting=None,
            configuration=dict(environment="real", video_source="device", video_endpoint="/dev/video0"),
            video=dict(source_id="camera", source="device", state="recent", endpoint="/dev/video0",
                       received_at=state["frame_received_at"], rx_age_s=state["frame_age_s"], age_limit_s=1.),
            vision=dict(configured=True, state="recent", frame_age_s=state["frame_age_s"],
                        age_limit_s=1., inference_ms=1.), yaw_preview=state)
        return validator.validate(snapshot, at, at, explicit_selection=explicit)

    assert demand(1.6, True).valid
    preview.observe(observation(3, 1.8, center=.6, track_id=9), 1.8)
    assert demand(1.8).reason == "target_paused"
    preview.observe(observation(4, 1.9, center=.65, track_id=9), 1.9)
    assert preview.state(1.9)["phase"] == "tracking"
    assert demand(1.9).reason == "target_recovering"
    assert not demand(1.91).valid
    preview.observe(observation(5, 2., center=.7, track_id=9), 2.)
    assert demand(2.).valid


def test_scalar_estimate_and_diagnostics_snapshots_cannot_mutate_later_authority():
    events = []
    preview = seed(on_recovery=events.append)
    estimate = preview._held_motion
    with pytest.raises(FrozenInstanceError):
        estimate.velocity_x = 100
    with pytest.raises(FrozenInstanceError):
        estimate.anchor.received_at = 100
    preview.observe(observation(3, 1.8, center=.6, track_id=9), 1.8)
    original = json.loads(json.dumps(events[0]))
    assert all(value is None or isinstance(value, (str, int, float, bool))
               for value in original.values())
    events[0]["motion_anchor_received_at"] = 100
    events[0]["motion_velocity_x"] = 100
    preview.observe(observation(4, 1.9, center=.65, track_id=9), 1.9)
    assert preview.state(1.9)["phase"] == "tracking"
    assert events[-1]["motion_anchor_received_at"] == 1.6
    assert events[-1]["motion_velocity_x"] == pytest.approx(.5)
    assert estimate.anchor.received_at == 1.6


def test_new_metadata_is_explicit_without_changing_existing_policy_metadata():
    for policy in ("motion", "motion_duplicates"):
        metadata = make_preview(policy).metadata
        assert metadata["version"] == 1
        assert "prediction_uncertainty_x_per_s" not in metadata
        assert "motion_estimate" not in metadata
    for policy in POLICIES:
        metadata = make_preview(policy).metadata
        assert metadata["version"] == 2 and metadata["offline_only"] is True
        assert metadata["prediction_max_horizon_s"] == .5
        assert metadata["prediction_uncertainty_x_per_s"] == .1
        assert metadata["production_recovery_gates"] == "unchanged_except_horizontal_reference"
