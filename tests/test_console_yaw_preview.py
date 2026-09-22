"""Image-only yaw suggestions must never outlive their selected observation."""
from copy import deepcopy
import math

import pytest

from argos.console.yaw_preview import (
    DEADBAND, FRAME_MAX_AGE, MAX_SAFE_INTEGER, YAW_LIMIT, YawPreview,
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


@pytest.mark.parametrize("loss", ["missing", "different", "low_confidence", "unavailable"])
def test_lost_target_does_not_reacquire_or_jump_to_another_person(loss):
    preview = selected()
    frame = observation(sequence=2, at=1.1)
    if loss == "missing":
        frame["detections"] = []
    elif loss == "different":
        frame["detections"][0]["track_id"] = 8
    elif loss == "low_confidence":
        frame["detections"][0]["confidence"] = .499
    else:
        frame = None
    preview.observe(frame, now=1.1)
    revision = assert_stopped(preview, 1.1)["revision"]
    preview.observe(observation(sequence=3, at=1.2), now=1.2)
    assert assert_stopped(preview, 1.2)["revision"] == revision


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
