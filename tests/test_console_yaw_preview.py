"""Image-only yaw suggestions must never outlive their selected observation."""
from copy import deepcopy
import math

import pytest

from argos.console.yaw_preview import (
    DEADBAND, FRAME_MAX_AGE, MAX_SAFE_INTEGER, RECOVERY_MAX_GAP, YAW_LIMIT,
    YawPreview,
)


def observation(*, sequence=1, at=1., center=.7, track_id=7, confidence=.9,
                run_id="run", video_id="camera", width=640, height=480):
    return {"run_id": run_id, "video_id": video_id, "sequence": sequence,
            "received_at": at, "width": width, "height": height,
            "detections": [{"track_id": track_id,
                            "box": [center - .025, .2, .05, .5],
                            "confidence": confidence}]}


def selected(**kwargs):
    preview = YawPreview(True)
    preview.observe(observation(**kwargs), now=1.)
    preview.select(kwargs.get("track_id", 7), revision=preview.revision, now=1.)
    return preview


def assert_stopped(preview, now):
    state = preview.state(now)
    assert state["phase"] == "stopped"
    assert state["target_id"] is None
    assert state["yaw"] == 0.
    assert state["error_x"] is None
    return state


@pytest.mark.parametrize("center,error,yaw", [
    (.5, 0., 0.), (.51, .02, 0.), (.49, -.02, 0.),
    (.6, .2, .05), (.4, -.2, -.05),
    (.9, .8, YAW_LIMIT), (.1, -.8, -YAW_LIMIT),
])
def test_horizontal_error_sign_deadband_and_bounded_stick_suggestion(center, error, yaw):
    state = selected(center=center).state(1.)
    assert state["phase"] == "tracking"
    assert state["error_x"] == pytest.approx(error)
    assert state["yaw"] == pytest.approx(yaw)
    assert state["yaw_limit"] == YAW_LIMIT
    assert state["deadband"] == DEADBAND
    assert state["frame_max_age_s"] == FRAME_MAX_AGE


def test_selection_is_explicit_and_does_not_require_any_transport():
    preview = YawPreview(True)
    preview.observe(observation(), now=1.)
    assert preview.state(1.)["phase"] == "idle"
    assert preview.state(1.)["yaw"] == 0.
    state = preview.select(7, revision=0, now=1.)
    assert state["revision"] == 1
    assert state["target_id"] == 7
    assert state["run_id"] == "run"
    assert state["video_id"] == "camera"
    assert state["frame_sequence"] == 1
    assert state["frame_received_at"] == 1.
    assert "no commands sent" in state["detail"]


def test_selection_epoch_survives_short_pause_but_not_reselect_clear_or_terminal_loss():
    preview = selected()
    epoch = preview.state(1.)["selection_epoch"]
    preview.observe(observation(sequence=2, at=1.1, confidence=.49), now=1.1)
    assert preview.state(1.1)["selection_epoch"] == epoch
    preview.observe(observation(sequence=3, at=1.2), now=1.2)
    assert preview.state(1.2)["selection_epoch"] == epoch
    preview.select(7, revision=preview.revision, now=1.2)
    assert preview.state(1.2)["selection_epoch"] == epoch + 1
    preview.clear()
    assert preview.state(1.2)["selection_epoch"] == epoch + 2
    preview.select(7, revision=preview.revision, now=1.2)
    assert preview.state(1.2)["selection_epoch"] == epoch + 3
    assert assert_stopped(preview, 1.7)["selection_epoch"] == epoch + 4
    assert assert_stopped(preview, 1.8)["selection_epoch"] == epoch + 4


def test_source_change_invalidates_selection_epoch_even_while_idle():
    preview = YawPreview(True)
    preview.observe(observation(), now=1.)
    epoch = preview.state(1.)["selection_epoch"]
    preview.observe(observation(sequence=2, at=1.1, video_id="new"), now=1.1)
    assert preview.state(1.1)["selection_epoch"] == epoch + 1


def test_disabled_never_uses_observations():
    preview = YawPreview(False)
    preview.observe(observation(), now=1.)
    state = preview.state(1.)
    assert state["phase"] == "disabled"
    assert state["yaw"] == 0.
    assert state["frame_sequence"] is None
    with pytest.raises(RuntimeError, match="disabled"):
        preview.select(7, revision=0, now=1.)


def test_state_expiry_is_latched_without_tick_and_cannot_be_renewed_by_polling():
    preview = selected()
    assert preview.state(1. + FRAME_MAX_AGE)["phase"] == "tracking"
    expired = assert_stopped(preview, 1. + FRAME_MAX_AGE + .001)
    revision = expired["revision"]
    preview.observe(observation(), now=1.5)
    assert assert_stopped(preview, 1.6)["revision"] == revision
    preview.observe(observation(sequence=2, at=1.7), now=1.7)
    assert assert_stopped(preview, 1.7)["revision"] == revision
    preview.select(7, revision=revision, now=1.7)
    assert preview.state(1.7)["phase"] == "tracking"


def test_a_fresh_completed_frame_is_used_before_deciding_target_freshness():
    preview = selected()
    preview.observe(observation(sequence=2, at=1.3, center=.3), now=1.51)
    state = preview.state(1.51)
    assert state["phase"] == "tracking"
    assert state["frame_sequence"] == 2
    assert state["frame_age_s"] == pytest.approx(.21)
    assert state["yaw"] == pytest.approx(-.1)


def test_distinct_fresh_images_update_only_the_selected_identity():
    preview = selected()
    frame = observation(sequence=2, at=1.2, center=.3)
    frame["detections"].append({"track_id": 9, "box": [.8, .2, .1, .4],
                                "confidence": .99})
    preview.observe(frame, now=1.3)
    state = preview.state(1.3)
    assert state["target_id"] == 7
    assert state["yaw"] == pytest.approx(-.1)
    assert state["frame_age_s"] == pytest.approx(.1)
    assert state["revision"] == 1


@pytest.mark.parametrize("loss", ["missing", "different", "low_confidence"])
def test_short_detection_gap_pauses_at_zero_and_recovers_only_the_same_id(loss):
    preview = selected()
    revision = preview.revision
    frame = observation(sequence=2, at=1.1)
    if loss == "missing":
        frame["detections"] = []
    elif loss == "different":
        frame["detections"][0]["track_id"] = 8
    elif loss == "low_confidence":
        frame["detections"][0]["confidence"] = .499
    preview.observe(frame, now=1.1)
    state = preview.state(1.1)
    assert state["phase"] == "paused"
    assert state["revision"] == revision + 1 and state["target_id"] == 7
    assert state["yaw"] == 0 and state["error_x"] is None
    assert state["recovery_deadline_at"] == 1. + RECOVERY_MAX_GAP
    preview.observe(observation(sequence=3, at=1.2, center=.3), now=1.2)
    state = preview.state(1.2)
    assert state["phase"] == "tracking"
    assert state["revision"] == revision + 1 and state["target_id"] == 7
    assert state["yaw"] == pytest.approx(-.1)
    assert state["recovery_deadline_at"] is None


def test_missing_image_still_stops_instead_of_pausing_detection_loss():
    preview = selected()
    preview.observe(None, now=1.1)
    revision = assert_stopped(preview, 1.1)["revision"]
    preview.observe(observation(sequence=3, at=1.2), now=1.2)
    assert assert_stopped(preview, 1.2)["revision"] == revision


@pytest.mark.parametrize("confidence", [0., .499])
def test_new_weak_images_and_polling_cannot_extend_recovery(confidence):
    preview = selected()
    revision = preview.revision
    for sequence, at in enumerate((1.1, 1.3, 1.5, 1.69), start=2):
        frame = observation(sequence=sequence, at=at, confidence=confidence)
        preview.observe(frame, now=at)
        for now in (at, at + .001):
            state = preview.state(now)
            assert state["phase"] == "paused"
            assert state["recovery_deadline_at"] == 1. + RECOVERY_MAX_GAP
            assert state["revision"] == revision + 1
            assert state["yaw"] == 0 and state["error_x"] is None
    state = assert_stopped(preview, 1. + RECOVERY_MAX_GAP + .001)
    assert state["revision"] == revision + 2
    preview.observe(observation(sequence=6, at=1.8), now=1.8)
    assert assert_stopped(preview, 1.8)["revision"] == revision + 2


def test_pause_stays_zero_for_other_people_until_original_selection_expires():
    preview = selected()
    for sequence, at in enumerate((1.1, 1.3, 1.5, 1.69), start=2):
        preview.observe(observation(sequence=sequence, at=at, track_id=8), now=at)
        state = preview.state(at)
        assert state["phase"] == "paused" and state["target_id"] == 7
        assert state["yaw"] == 0 and state["error_x"] is None
    assert_stopped(preview, 1. + RECOVERY_MAX_GAP + .001)


def test_pause_budget_starts_at_last_strong_source_not_detection_or_click_time():
    preview = YawPreview(True)
    preview.observe(observation(), now=1.2)
    preview.select(7, revision=0, now=1.3)
    frame = observation(sequence=2, at=1.4)
    frame["detections"] = []
    preview.observe(frame, now=1.5)
    assert preview.state(1.5)["recovery_deadline_at"] == 1. + RECOVERY_MAX_GAP


def test_successful_recovery_supplies_a_new_bounded_gap():
    preview = selected()
    preview.observe(observation(sequence=2, at=1.1, confidence=.49), now=1.1)
    preview.observe(observation(sequence=3, at=1.2), now=1.3)
    preview.observe(observation(sequence=4, at=1.4, confidence=.49), now=1.5)
    state = preview.state(1.5)
    assert state["recovery_deadline_at"] == 1.2 + RECOVERY_MAX_GAP
    assert state["revision"] == 3


def test_same_identity_with_old_source_time_cannot_resume_paused_output():
    preview = selected()
    preview.observe(observation(sequence=2, at=1., confidence=.49), now=1.1)
    preview.observe(observation(sequence=3, at=1.), now=1.2)
    state = preview.state(1.2)
    assert state["phase"] == "paused"
    assert state["yaw"] == 0 and state["error_x"] is None
    assert state["recovery_deadline_at"] == 1. + RECOVERY_MAX_GAP


@pytest.mark.parametrize("arrival", [1. + RECOVERY_MAX_GAP,
                                     1. + RECOVERY_MAX_GAP + .001])
def test_late_delivery_cannot_resume_at_or_after_recovery_deadline(arrival):
    preview = selected()
    preview.observe(observation(sequence=2, at=1.2, confidence=.49), now=1.2)
    preview.observe(observation(sequence=3, at=1.6), now=arrival)
    assert_stopped(preview, arrival)


def test_paused_revision_fences_requests_created_before_detection_gap():
    preview = selected()
    selected_revision = preview.revision
    preview.observe(observation(sequence=2, at=1.1, confidence=.49), now=1.1)
    preview.observe(observation(sequence=3, at=1.2), now=1.2)
    with pytest.raises(RuntimeError, match="changed"):
        preview.select(7, revision=selected_revision, now=1.2)
    assert preview.state(1.2)["phase"] == "tracking"
    assert preview.revision == selected_revision + 1


def test_pause_stops_when_image_goes_stale_before_detection_recovery_deadline():
    preview = selected()
    preview.observe(observation(sequence=2, at=1.1, confidence=.49), now=1.1)
    state = assert_stopped(preview, 1.1 + FRAME_MAX_AGE + .001)
    assert state["detail"] == "Selected target image is stale"


def test_explicit_clear_during_pause_forbids_automatic_recovery():
    preview = selected()
    preview.observe(observation(sequence=2, at=1.1, confidence=.49), now=1.1)
    preview.clear()
    preview.observe(observation(sequence=3, at=1.2), now=1.2)
    state = preview.state(1.2)
    assert state["phase"] == "idle" and state["target_id"] is None
    assert state["yaw"] == 0 and state["error_x"] is None
    assert state["revision"] == 3 and state["recovery_deadline_at"] is None


@pytest.mark.parametrize("fault", ["unavailable", "context", "order", "clock", "metadata"])
def test_image_integrity_failure_during_pause_still_latches_stop(fault):
    preview = selected()
    preview.observe(observation(sequence=2, at=1.1, confidence=.49), now=1.1)
    frame, now = observation(sequence=3, at=1.2), 1.2
    if fault == "unavailable":
        frame = None
    elif fault == "context":
        frame["video_id"] = "another camera"
    elif fault == "order":
        frame["sequence"] = 1
    elif fault == "clock":
        now = 1.05
    else:
        frame["width"] = 0
    preview.observe(frame, now=now)
    revision = assert_stopped(preview, 1.2)["revision"]
    preview.observe(observation(sequence=4, at=1.3), now=1.3)
    assert preview.state(1.3)["phase"] == "stopped"
    assert preview.state(1.3)["yaw"] == 0
    assert preview.revision >= revision


@pytest.mark.parametrize("change,value", [
    ("run_id", "another run"), ("video_id", "another camera"),
    ("width", 1280), ("height", 720),
])
def test_context_and_dimensions_reset_even_if_track_id_is_reused(change, value):
    preview = selected()
    frame = observation(sequence=1, at=1.1)
    frame[change] = value
    preview.observe(frame, now=1.1)
    state = assert_stopped(preview, 1.1)
    assert state["revision"] == 2
    with pytest.raises(RuntimeError, match="changed"):
        preview.select(7, revision=1, now=1.1)
    preview.select(7, revision=state["revision"], now=1.1)
    assert preview.state(1.1)["phase"] == "tracking"


def test_source_change_invalidates_selection_queued_while_idle():
    preview = YawPreview(True)
    preview.observe(observation(), now=1.)
    preview.observe(observation(video_id="next", at=1.1), now=1.1)
    with pytest.raises(RuntimeError, match="changed"):
        preview.select(7, revision=0, now=1.1)


@pytest.mark.parametrize("change", ["sequence", "receipt", "repeated_receipt", "repeated_box"])
def test_regressing_or_mutated_image_identity_stops_and_remains_unselectable(change):
    preview = selected(sequence=2)
    frame = observation(sequence=3, at=1.1)
    if change == "sequence":
        frame["sequence"] = 1
    elif change == "receipt":
        frame["received_at"] = .9
    elif change == "repeated_receipt":
        frame["sequence"] = 2
    else:
        frame["sequence"] = 2
        frame["received_at"] = 1.
        frame["detections"][0]["box"][0] = .1
    preview.observe(frame, now=1.1)
    state = assert_stopped(preview, 1.1)
    with pytest.raises(RuntimeError, match="No recent"):
        preview.select(7, revision=state["revision"], now=1.1)


def test_missing_image_does_not_erase_the_sequence_high_water_mark():
    preview = selected(sequence=3)
    preview.observe(None, now=1.1)
    preview.observe(observation(sequence=2, at=1.2), now=1.2)
    state = assert_stopped(preview, 1.2)
    assert state["frame_sequence"] is None


def test_clear_invalidates_in_flight_select_and_does_not_automatically_resume():
    preview = selected()
    old_revision = preview.revision
    preview.clear()
    state = preview.state(1.1)
    assert state["phase"] == "idle"
    assert state["target_id"] is None
    assert state["yaw"] == 0.
    assert state["revision"] == old_revision + 1
    assert state["frame_sequence"] == 1
    with pytest.raises(RuntimeError, match="changed"):
        preview.select(7, revision=old_revision, now=1.1)
    preview.clear()
    assert preview.revision == old_revision + 2


def test_selection_checks_expiry_before_matching_revision():
    preview = selected()
    revision = preview.revision
    with pytest.raises(RuntimeError, match="changed"):
        preview.select(7, revision=revision, now=1.5)
    assert_stopped(preview, 1.5)


@pytest.mark.parametrize("revision", [True, 1., -1, "1", None])
def test_revision_has_strict_integer_type(revision):
    preview = selected()
    with pytest.raises(RuntimeError, match="changed"):
        preview.select(7, revision=revision, now=1.)


@pytest.mark.parametrize("identity", [True, 7., 0, -1, MAX_SAFE_INTEGER + 1])
def test_browser_target_identity_has_strict_safe_integer_type(identity):
    preview = selected()
    with pytest.raises(ValueError, match="identity"):
        preview.select(identity, revision=1, now=1.)


@pytest.mark.parametrize("field,value", [
    ("sequence", True), ("sequence", 0), ("sequence", MAX_SAFE_INTEGER + 1),
    ("width", True), ("height", 0), ("width", 4097),
    ("received_at", math.nan), ("received_at", math.inf),
    ("received_at", True), ("received_at", -1.), ("received_at", 1.2),
    ("video_id", ""), ("detections", {}),
])
def test_invalid_image_metadata_stops_preview(field, value):
    preview = selected()
    frame = observation(sequence=2, at=1.1)
    frame[field] = value
    preview.observe(frame, now=1.1)
    assert_stopped(preview, 1.1)


@pytest.mark.parametrize("field,value", [
    ("track_id", True), ("track_id", 0), ("track_id", MAX_SAFE_INTEGER + 1),
    ("confidence", math.nan), ("confidence", True), ("confidence", 1.1),
    ("box", [True, .2, .2, .3]), ("box", [.2, .2, 0., .3]),
    ("box", [.9, .2, .2, .3]), ("box", [.2, .2, .2, math.inf]),
    ("box", [.2, .2, .2]),
])
def test_invalid_detection_metadata_stops_preview(field, value):
    preview = selected()
    frame = observation(sequence=2, at=1.1)
    frame["detections"][0][field] = value
    preview.observe(frame, now=1.1)
    assert_stopped(preview, 1.1)


def test_duplicate_ids_are_ambiguous_and_clear_the_preview():
    preview = selected()
    frame = observation(sequence=2, at=1.1)
    frame["detections"].append(deepcopy(frame["detections"][0]))
    preview.observe(frame, now=1.1)
    assert_stopped(preview, 1.1)


def test_caller_mutation_cannot_change_retained_target_geometry():
    preview = YawPreview(True)
    frame = observation()
    preview.observe(frame, now=1.)
    frame["detections"][0]["box"][0] = .05
    frame["detections"].clear()
    state = preview.select(7, revision=0, now=1.)
    assert state["yaw"] == pytest.approx(.1)


def test_clock_regression_stops_without_allowing_an_old_frame_to_resume():
    preview = selected()
    preview.state(1.2)
    assert_stopped(preview, 1.1)
    preview.observe(observation(sequence=2, at=1.15), now=1.15)
    with pytest.raises(RuntimeError, match="backwards"):
        preview.select(7, revision=preview.revision, now=1.15)
    assert_stopped(preview, 1.2)


@pytest.mark.parametrize("now", [True, -1, math.nan, math.inf])
def test_invalid_clock_fails_closed(now):
    preview = selected()
    with pytest.raises(ValueError, match="session time"):
        preview.state(now)
    assert_stopped(preview, 1.)


def fly_observation(*, appearance=True, **kwargs):
    frame = observation(**kwargs)
    descriptor = [1.] + [0.] * 207
    frame["appearances"] = [descriptor if appearance else None]
    return frame


def fly_selected():
    preview = YawPreview(True, continuous=True)
    preview.observe(fly_observation(), now=1.)
    preview.select(7, revision=0, now=1.)
    return preview


@pytest.mark.parametrize("center,expected", [(.5, 0.), (.51, 0.), (.6, .1), (.75, .2), (.25, -.2)])
def test_continuous_gain_and_twenty_percent_limit_do_not_change_diagnostic_preview(center, expected):
    fly = YawPreview(True, continuous=True)
    fly.observe(fly_observation(center=center), now=1.)
    result = fly.select(7, revision=0, now=1.)
    assert result["yaw"] == pytest.approx(expected)
    assert result["yaw_limit"] == .2
    diagnostic = YawPreview(True)
    diagnostic.observe(fly_observation(center=center), now=1.)
    old = diagnostic.select(7, revision=0, now=1.)
    assert old["yaw_limit"] == .125
    assert abs(old["yaw"]) <= .125
    fly.observe(None, now=1.1)
    assert fly.state(1.1)["yaw"] == 0.


def test_continuous_recovery_remembers_three_seconds_without_refreshing_absence():
    preview = fly_selected()
    epoch = preview.selection_epoch
    for seq, at in enumerate((1.1, 1.7, 2.4, 3.6), start=2):
        frame = observation(sequence=seq, at=at)
        frame["detections"] = []
        preview.observe(frame, now=at)
        state = preview.state(at)
        assert state["phase"] == "paused" and state["target_id"] == 7
        assert state["recovery_deadline_at"] == 4.
        assert state["selection_epoch"] == epoch
        assert state["yaw"] == 0
    preview.observe(fly_observation(sequence=6, at=3.7), now=3.7)
    state = preview.state(3.7)
    assert state["phase"] == "tracking" and state["selection_epoch"] == epoch


def test_unique_appearance_recovery_survives_tracker_id_change_and_requires_new_images():
    preview = fly_selected()
    epoch = preview.selection_epoch
    frame = observation(sequence=2, at=1.1)
    frame["detections"] = []
    preview.observe(frame, now=1.1)
    frame = fly_observation(sequence=3, at=2., track_id=81, center=.72)
    preview.observe(frame, now=2.)
    assert preview.state(2.)["phase"] == "paused"
    assert preview.state(2.01)["phase"] == "paused"
    preview.observe(frame, now=2.02)
    assert preview.state(2.02)["phase"] == "paused"
    preview.observe(fly_observation(sequence=4, at=2.1, track_id=81, center=.73), now=2.1)
    state = preview.state(2.1)
    assert state["phase"] == "tracking" and state["target_id"] == 81
    assert state["selection_id"] == 7 and state["selection_epoch"] == epoch


@pytest.mark.parametrize("reason", ["missing_appearance", "different_appearance", "far_geometry", "changing_id"])
def test_single_visible_person_alone_cannot_prove_identity(reason):
    preview = fly_selected()
    for seq, at in enumerate((2., 2.1, 2.2), start=2):
        frame = fly_observation(sequence=seq, at=at, track_id=81 + (seq if reason == "changing_id" else 0),
                                center=.4 if reason == "far_geometry" else .72,
                                appearance=reason != "missing_appearance")
        if reason == "different_appearance":
            frame["appearances"] = [[0., 1.] + [0.] * 206]
        preview.observe(frame, now=at)
        assert preview.state(at)["phase"] == "paused"
        assert preview.state(at)["target_id"] == 7


def test_ambiguity_latches_manual_even_when_original_person_returns_later():
    preview = fly_selected()
    frame = observation(sequence=2, at=1.1, track_id=81)
    frame["detections"].append({"track_id": 82, "box": [.4, .2, .1, .5], "confidence": .9})
    preview.observe(frame, now=1.1)
    assert "ambiguous" in assert_stopped(preview, 1.1)["detail"]
    preview.observe(fly_observation(sequence=3, at=1.2), now=1.2)
    assert_stopped(preview, 1.2)
    assert preview.select_center(now=1.2)["target_id"] == 7


def test_continuous_long_loss_never_resumes_even_if_no_state_was_polled():
    preview = fly_selected()
    preview.observe(fly_observation(sequence=2, at=4.), now=4.)
    assert_stopped(preview, 4.)
    assert preview.select_center(now=4.)["phase"] == "tracking"


def test_radio_selection_keeps_valid_target_and_chooses_nearest_center_after_loss():
    preview = fly_selected()
    initial = preview.state(1.)
    frame = observation(sequence=2, at=1.1)
    frame["detections"].append({"track_id": 9, "box": [.45, .25, .1, .5], "confidence": .9})
    preview.observe(frame, now=1.1)
    retained = preview.select_center(now=1.1)
    assert retained["target_id"] == 7
    assert retained["selection_epoch"] == initial["selection_epoch"]
    preview.clear()
    chosen = preview.select_center(now=1.1)
    assert chosen["target_id"] == chosen["selection_id"] == 9
    assert chosen["selection_epoch"] == initial["selection_epoch"] + 2


def test_radio_selection_rejects_near_ties_and_does_not_wait_for_future_candidates():
    preview = YawPreview(True, continuous=True)
    frame = observation()
    frame["detections"].append({"track_id": 9, "box": [.28, .2, .05, .5], "confidence": .9})
    preview.observe(frame, now=1.)
    with pytest.raises(RuntimeError, match="ambiguous"):
        preview.select_center(now=1.)
    frame = observation(sequence=2, at=1.1)
    frame["detections"] = []
    preview.observe(frame, now=1.1)
    with pytest.raises(RuntimeError, match="No confident"):
        preview.select_center(now=1.1)
    preview.observe(observation(sequence=3, at=1.2), now=1.2)
    assert preview.state(1.2)["target_id"] is None
    with pytest.raises(RuntimeError, match="No recent"):
        preview.select_center(now=1.7)


def recovery_candidate(sequence, at, *, score=.90, dx=.25, dy=.15, confidence=.9):
    frame = fly_observation(sequence=sequence, at=at, track_id=81, confidence=confidence)
    frame["appearances"] = [[score, math.sqrt(1 - score * score)] + [0.] * 206]
    frame["detections"][0]["box"] = [.234375 + dx, .21875 + dy, .03125, .0625]
    return frame


def recovery_reference(callback=None, *, continuous=True):
    preview = YawPreview(True, continuous=continuous, on_recovery=callback)
    frame = fly_observation()
    frame["detections"][0]["box"] = [.234375, .21875, .03125, .0625]
    preview.observe(frame, now=1.)
    preview.select(7, revision=0, now=1.)
    return preview


def test_missing_same_track_appearance_keeps_recent_descriptor_and_its_original_box():
    records = []
    preview = recovery_reference(records.append)
    # Tracking still uses the latest crop, whose bounds differ from the last
    # usable appearance crop. Recovery must retain the descriptor's own bounds.
    preview.observe(fly_observation(sequence=2, at=1.1, center=.55, appearance=False), now=1.1)
    state = preview.state(1.1)
    assert state["phase"] == "tracking" and state["yaw"] == pytest.approx(.05)
    for seq, at in ((3, 1.2), (4, 1.3)):
        preview.observe(recovery_candidate(seq, at, dx=.1, dy=.05), now=at)
    assert preview.state(1.3)["target_id"] == 81
    assert [record["reason"] for record in records] == ["pending_second_image", "accepted_appearance"]
    assert records[-1]["width_ratio"] == records[-1]["height_ratio"] == 1.
    assert records[-1]["dx"] == pytest.approx(.1)
    assert records[-1]["dy"] == pytest.approx(.05)
    assert records[-1]["reference_appearance_available"] is True
    assert records[-1]["candidate_appearance_available"] is True
    assert records[-1]["reference_appearance_age_s"] == pytest.approx(.3)


@pytest.mark.parametrize("second_at", [4., 4.01])
def test_missing_appearance_frames_and_polls_cannot_renew_recovery_reference(second_at):
    records = []
    preview = recovery_reference(records.append)
    preview.state(1.2)  # Polling the valid crop cannot refresh its receipt time.
    for seq, at in enumerate((1.5, 2., 2.5, 3., 3.5, 3.8), start=2):
        preview.observe(fly_observation(sequence=seq, at=at, appearance=False), now=at)
        assert preview.state(at)["phase"] == "tracking"
    preview.observe(recovery_candidate(8, 3.9), now=3.9)
    assert records[-1]["reason"] == "pending_second_image"
    preview.state(3.95)
    preview.observe(recovery_candidate(9, second_at), now=second_at)
    state = preview.state(second_at)
    assert state["phase"] == "paused" and state["target_id"] == 7 and state["yaw"] == 0.
    assert state["recovery_deadline_at"] == pytest.approx(6.8)
    assert records[-1]["reason"] == "reference_appearance_expired"
    assert records[-1]["reference_appearance_available"] is True
    assert records[-1]["candidate_appearance_available"] is True
    assert records[-1]["reference_appearance_age_s"] == pytest.approx(second_at - 1.)


def test_new_usable_same_track_crop_renews_reference_from_its_own_receipt():
    records = []
    preview = recovery_reference(records.append)
    for seq, at in enumerate((1.5, 2., 2.5, 3., 3.5), start=2):
        preview.observe(fly_observation(sequence=seq, at=at, appearance=False), now=at)
    renewed = recovery_candidate(7, 3.8, dx=0., dy=0., score=1.)
    renewed["detections"][0]["track_id"] = 7
    preview.observe(renewed, now=3.9)
    for seq, at in ((8, 4.), (9, 4.1)):
        preview.observe(recovery_candidate(seq, at), now=at)
    assert preview.state(4.1)["target_id"] == 81
    assert records[-1]["reason"] == "accepted_appearance"
    assert records[-1]["reference_appearance_age_s"] == pytest.approx(.3)


def test_expired_appearance_does_not_block_existing_same_track_recovery():
    records = []
    preview = recovery_reference(records.append)
    for seq, at in enumerate((1.5, 2., 2.5, 3., 3.5, 3.8), start=2):
        preview.observe(fly_observation(sequence=seq, at=at, appearance=False), now=at)
    missing = observation(sequence=8, at=3.9)
    missing["detections"] = []
    preview.observe(missing, now=3.9)
    assert preview.state(3.9)["phase"] == "paused"
    preview.observe(fly_observation(sequence=9, at=4., appearance=False), now=4.)
    assert preview.state(4.)["phase"] == "tracking"
    assert preview.state(4.)["target_id"] == 7
    assert records[-1]["reason"] == "accepted_same_track"
    assert records[-1]["reference_appearance_age_s"] == 3.


@pytest.mark.parametrize("reset", ["reselect", "other_target", "clear", "run", "video", "dimensions"])
def test_cached_appearance_never_crosses_explicit_selection_or_image_context(reset):
    records = []
    preview = recovery_reference(records.append)
    kwargs = {"sequence": 2, "at": 1.1, "appearance": False}
    if reset == "other_target":
        kwargs["track_id"] = 9
    elif reset == "run":
        kwargs["run_id"] = "other-run"
    elif reset == "video":
        kwargs["video_id"] = "other-camera"
    elif reset == "dimensions":
        kwargs["width"] = 320
    elif reset == "clear":
        preview.clear()
    frame = fly_observation(**kwargs)
    preview.observe(frame, now=1.1)
    preview.select(kwargs.get("track_id", 7), revision=preview.revision, now=1.1)
    records.clear()
    for seq, at in ((3, 1.2), (4, 1.3)):
        candidate = recovery_candidate(seq, at)
        for field in ("run_id", "video_id", "width"):
            candidate[field] = frame[field]
        preview.observe(candidate, now=at)
    assert preview.state(1.3)["phase"] == "paused"
    assert len(records) == 2
    assert all(record["reason"] == "appearance_unavailable" for record in records)
    assert all(record["reference_appearance_available"] is False for record in records)
    assert all(record["candidate_appearance_available"] is True for record in records)
    assert all(record["reference_appearance_age_s"] is None for record in records)


def test_cached_reference_cannot_replace_missing_candidate_appearance():
    records = []
    preview = recovery_reference(records.append)
    preview.observe(fly_observation(sequence=2, at=1.1, appearance=False), now=1.1)
    for seq, at in ((3, 1.2), (4, 1.3)):
        candidate = recovery_candidate(seq, at)
        candidate["appearances"] = [None]
        preview.observe(candidate, now=at)
    assert preview.state(1.3)["phase"] == "paused"
    assert all(record["reason"] == "appearance_unavailable" for record in records)
    assert all(record["reference_appearance_available"] is True for record in records)
    assert all(record["candidate_appearance_available"] is False for record in records)
    assert records[-1]["reference_appearance_age_s"] == pytest.approx(.3)


@pytest.mark.parametrize("score,accepted", [(.86, False), (.88, True), (.90, True)])
def test_continuous_similarity_threshold_is_separate_and_keeps_two_image_gate(score, accepted):
    from argos.perception.appearance import MIN_SIMILARITY
    records = []
    preview = recovery_reference(records.append)
    preview.observe(recovery_candidate(2, 1.1, score=score), now=1.1)
    assert preview.state(1.1)["phase"] == "paused"
    preview.observe(recovery_candidate(3, 1.2, score=score), now=1.2)
    assert preview.state(1.2)["phase"] == ("tracking" if accepted else "paused")
    assert records[-1]["accepted"] is accepted
    assert records[-1]["reason"] == ("accepted_appearance" if accepted else "appearance_similarity")
    assert records[-1]["similarity"] == pytest.approx(score)
    assert MIN_SIMILARITY == .95  # The ordinary appearance association is unchanged.


@pytest.mark.parametrize("dx,dy,reason", [(.25, .15, "accepted_appearance"),
                                        (.2501, .15, "horizontal_distance"),
                                        (.25, .1501, "vertical_distance")])
def test_continuous_geometry_uses_image_distance_without_small_box_relative_caps(dx, dy, reason):
    records = []
    preview = recovery_reference(records.append)
    for seq, at in ((2, 1.1), (3, 1.2)):
        preview.observe(recovery_candidate(seq, at, dx=dx, dy=dy), now=at)
    assert records[-1]["reason"] == reason
    assert records[-1]["dx"] == pytest.approx(dx)
    assert records[-1]["dy"] == pytest.approx(dy)
    assert records[-1]["width_ratio"] == records[-1]["height_ratio"] == 1.
    assert preview.state(1.2)["phase"] == ("tracking" if reason == "accepted_appearance" else "paused")


def test_relaxed_continuous_recovery_does_not_change_the_diagnostic_preview():
    records = []
    preview = recovery_reference(records.append, continuous=False)
    preview.observe(recovery_candidate(2, 1.1), now=1.1)
    preview.observe(recovery_candidate(3, 1.2), now=1.2)
    assert preview.state(1.2)["phase"] == "paused"
    assert preview.state(1.2)["target_id"] == 7
    assert preview.state(1.2)["recovery_max_gap_s"] == .7
    assert records == []


@pytest.mark.parametrize("axis,ratio,reason", [(2, .49, "width_ratio"), (2, 2.01, "width_ratio"),
                                               (3, .749, "height_ratio"), (3, 1.334, "height_ratio")])
def test_relaxed_distances_do_not_remove_size_compatibility(axis, ratio, reason):
    records = []
    preview = recovery_reference(records.append)
    for seq, at in ((2, 1.1), (3, 1.2)):
        frame = recovery_candidate(seq, at, dx=.1, dy=.05)
        frame["detections"][0]["box"][axis] *= ratio
        preview.observe(frame, now=at)
    assert preview.state(1.2)["phase"] == "paused"
    assert records[-1]["reason"] == reason and not records[-1]["accepted"]


def test_recovery_diagnostics_deduplicate_polls_and_contain_scalar_decision_evidence():
    records = []
    preview = recovery_reference(records.append)
    first = recovery_candidate(2, 1.1)
    preview.observe(first, now=1.1)
    for now in (1.11, 1.12, 1.13):
        preview.state(now)
        preview.observe(first, now=now)
    assert len(records) == 1 and records[0]["reason"] == "pending_second_image"
    assert records[0]["accepted"] is False
    preview.observe(recovery_candidate(3, 1.2), now=1.2)
    preview.state(1.21)
    assert len(records) == 2
    result = records[-1]
    assert result["event"] == "yaw_recovery" and result["reason"] == "accepted_appearance"
    assert result["run_id"] == "run" and result["video_id"] == "camera"
    assert result["frame_sequence"] == 3 and result["frame_received_at"] == 1.2
    assert result["selection_id"] == result["previous_track_id"] == 7
    assert result["selection_epoch"] == 1 and result["track_id"] == 81
    assert all(value is None or type(value) in (str, int, float, bool) for value in result.values())
    assert "appearance" not in result and "box" not in result


@pytest.mark.parametrize("failure", ["ambiguous", "low_confidence", "no_appearance", "no_candidate",
                                      "deadline", "stale"])
def test_recovery_diagnostics_report_refusal_cause_without_fabricating_metrics(failure):
    records = []
    preview = recovery_reference(records.append)
    frame = recovery_candidate(2, 1.1)
    now = 1.1
    if failure == "ambiguous":
        frame["detections"].append({**frame["detections"][0], "track_id": 82})
        frame["appearances"].append(frame["appearances"][0])
    elif failure == "low_confidence":
        frame["detections"][0]["confidence"] = .49
    elif failure == "no_appearance":
        frame["appearances"] = [None]
    elif failure == "no_candidate":
        frame["detections"], frame["appearances"] = [], []
    elif failure == "deadline":
        now = frame["received_at"] = 4.
    else:
        now = 1.6
    preview.observe(frame, now=now)
    reasons = {"ambiguous": "ambiguous_candidates", "low_confidence": "low_confidence",
               "no_appearance": "appearance_unavailable", "no_candidate": "no_candidate",
               "deadline": "recovery_deadline", "stale": "stale_image"}
    assert all(record["reason"] == reasons[failure] and not record["accepted"] for record in records)
    assert len(records) == (2 if failure == "ambiguous" else 1)
    if failure in ("no_appearance", "no_candidate"):
        assert records[0]["similarity"] is None
    if failure == "no_candidate":
        assert records[0]["dx"] is records[0]["dy"] is None


def test_recovery_callback_failure_cannot_change_acceptance_or_manual_gap():
    def failing(_record):
        raise OSError("logger unavailable")
    preview = recovery_reference(failing)
    preview.observe(recovery_candidate(2, 1.1), now=1.1)
    assert preview.state(1.1)["yaw"] == 0 and preview.state(1.1)["phase"] == "paused"
    preview.observe(recovery_candidate(3, 1.2), now=1.2)
    assert preview.state(1.2)["phase"] == "tracking"


def test_missing_image_diagnostics_require_an_existing_active_selection():
    records = []
    preview = YawPreview(True, continuous=True, on_recovery=records.append)
    preview.observe(None, now=0.)
    preview.observe({}, now=.1)
    assert records == []
    preview.observe(fly_observation(), now=1.)
    preview.select(7, revision=0, now=1.)
    preview.observe(None, now=1.1)
    assert len(records) == 1 and records[0]["reason"] == "image_unavailable"
    assert records[0]["selection_id"] == 7
    preview.observe(None, now=1.2)
    assert len(records) == 1
