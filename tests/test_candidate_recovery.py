"""Offline qualification must distinguish evidence from candidate counts."""
from copy import deepcopy
import json
import math

import pytest

from argos.backends.yaw_stream_source import YawValidator
from argos.perception.recovery_prototype import make_preview


A = [1.] + [0.] * 207
B = [0., 1.] + [0.] * 206
POLICY = "motion_held_candidates"


def descriptor(score):
    return [score, math.sqrt(1 - score * score)] + [0.] * 206


def detection(track_id=7, center=.2, confidence=.9, *, width=.1, y=.2, height=.4):
    return dict(track_id=track_id, box=[center - width / 2, y, width, height],
                confidence=confidence)


def observation(sequence, at, *, detections=None, appearances=None):
    return dict(run_id="run", video_id="camera", sequence=sequence, received_at=at,
                width=640, height=480,
                detections=[detection()] if detections is None else detections,
                appearances=[A] if appearances is None else appearances)


def seed(*, policy=POLICY, **kwargs):
    preview = make_preview(policy, **kwargs)
    preview.observe(observation(1, 1.), 1.)
    preview.select(7, revision=preview.revision, now=1.)
    preview.observe(observation(2, 1.2, detections=[detection(center=.4)],
                                appearances=[None]), 1.2)
    return preview


def pair(sequence=3, at=1.4, *, ids=(9, 11), scores=(.9, .99),
         appearances=None, center=None, reverse=False):
    center = at - .8 if center is None else center
    candidates = [detection(ids[0], center, scores[0]), detection(ids[1], center, scores[1])]
    appearances = [A, B] if appearances is None else list(appearances)
    if reverse:
        candidates.reverse()
        appearances.reverse()
    return observation(sequence, at, detections=candidates, appearances=appearances)


def qualification(events):
    return [event for event in events if event["event"] == "candidate_qualification"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("same_id", [False, True])
def test_unique_compatible_candidate_recovers_despite_high_confidence_other(reverse, same_id):
    events = []
    preview = seed(on_policy=events.append)
    if same_id:
        preview.observe(observation(3, 1.3, detections=[], appearances=[]), 1.3)
    target_id = 7 if same_id else 9
    revision, epoch = preview.revision, preview.selection_epoch
    reference, estimate = deepcopy(preview._reference), preview._held_motion
    first = pair(4, 1.4, ids=(target_id, 11), reverse=reverse)
    original = deepcopy(first)
    preview.observe(first, 1.4)
    state = preview.state(1.4)
    assert state["phase"] == "paused" and state["yaw"] == 0.
    assert state["selection_epoch"] == epoch
    assert state["revision"] == revision + (not same_id)
    assert preview._reference == reference and preview._reference_received_at == 1.
    assert preview._held_motion is estimate
    assert preview.preview_detection_indices == [0, 1]
    assert first == original
    preview.observe(pair(5, 1.5, ids=(target_id, 11), reverse=not reverse), 1.5)
    state = preview.state(1.5)
    assert state["phase"] == "tracking" and state["target_id"] == target_id
    assert state["selection_id"] == 7 and state["selection_epoch"] == epoch
    assert state["error_x"] == pytest.approx(.4)
    assert state["yaw"] == .2
    decisions = qualification(events)
    assert [event["decision"] for event in decisions] == ["pending_second_image", "accepted_appearance"]
    assert all(event["winner_track_id"] == target_id for event in decisions)
    assert all(event["reason"] == "unique_compatible_candidate" for event in decisions)


@pytest.mark.parametrize("appearances,reason", [
    ([A, descriptor(.9)], "multiple_eligible_candidates"),
    ([descriptor(.9), descriptor(.86)], "appearance_margin"),
    ([A, None], "unknown_plausible_competitor"),
    ([B, B], "no_eligible_candidate"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_unresolved_candidates_stay_zero_without_renewing_evidence(appearances, reason, reverse):
    events = []
    preview = seed(on_policy=events.append)
    reference, estimate = deepcopy(preview._reference), preview._held_motion
    for sequence, at in ((3, 1.4), (4, 1.5)):
        preview.observe(pair(sequence, at, appearances=appearances, reverse=reverse), at)
        state = preview.state(at)
        assert state["phase"] == "paused" and state["yaw"] == 0.
        assert state["recovery_deadline_at"] == 4.2
        assert preview._last_seen_at == 1.2
        assert preview._reference == reference
        assert preview._held_motion is estimate
        assert preview._recovery_candidate is None
        assert preview.preview_detection_indices == [0, 1]
    assert [event["reason"] for event in qualification(events)] == [reason, reason]


@pytest.mark.parametrize("winner,competitor,accepted", [
    (.9, .85, True), (.9, .8500000000002, True),
    (.9, .850000000002, False), (.9, .8501, False),
    (1., .89, False),  # Two eligible always ambiguous, despite a large margin.
])
def test_fixed_similarity_margin_boundary(winner, competitor, accepted):
    preview = seed()
    for sequence, at in ((3, 1.4), (4, 1.5)):
        preview.observe(pair(sequence, at, appearances=[descriptor(winner), descriptor(competitor)]), at)
    assert (preview.state(1.5)["phase"] == "tracking") is accepted


@pytest.mark.parametrize("control,reason", [
    ("missing_appearance", "appearance_unavailable"),
    ("wrong_appearance", "appearance_similarity"),
    ("wide", "width_ratio"), ("tall", "height_ratio"),
    ("horizontal", "horizontal_distance"), ("vertical", "vertical_distance"),
    ("weak", "low_confidence"),
])
def test_candidate_qualification_preserves_every_production_gate(control, reason):
    events = []
    preview = seed(on_policy=events.append)
    # Enter the competition episode before weakening the candidate so a later
    # reduction to one strong detection cannot delegate outside qualification.
    preview.observe(pair(3, 1.3, appearances=[A, A]), 1.3)
    for sequence, at in ((4, 1.4), (5, 1.5)):
        frame = pair(sequence, at)
        candidate = frame["detections"][0]
        if control == "missing_appearance":
            frame["appearances"][0] = None
        elif control == "wrong_appearance":
            frame["appearances"][0] = B
        elif control == "wide":
            candidate["box"][0], candidate["box"][2] = at - .8 - .125, .25
        elif control == "tall":
            candidate["box"][1], candidate["box"][3] = .05, .7
        elif control == "horizontal":
            candidate["box"][0] = .0
        elif control == "vertical":
            candidate["box"][1] = .36
        else:
            candidate["confidence"] = .499
        preview.observe(frame, at)
        assert preview.state(at)["phase"] == "paused"
        assert preview.state(at)["yaw"] == 0.
        assert preview._recovery_candidate is None
        assert qualification(events)[-1]["rows"][0]["reason"] == reason


def test_qualified_images_require_receipt_newer_than_last_selected_image():
    events = []
    preview = seed(on_policy=events.append)
    preview.observe(pair(3, 1.2, center=.4), 1.3)
    assert preview.state(1.3)["phase"] == "paused"
    assert qualification(events)[-1]["rows"][0]["reason"] == "old_image_receipt"
    assert preview._recovery_candidate is None


def test_motion_cannot_create_missing_reference_appearance():
    events = []
    preview = make_preview(POLICY, on_policy=events.append)
    preview.observe(observation(1, 1., appearances=[None]), 1.)
    preview.select(7, revision=preview.revision, now=1.)
    preview.observe(observation(2, 1.2, detections=[detection(center=.4)],
                                appearances=[None]), 1.2)
    preview.observe(pair(), 1.4)
    assert preview.state(1.4)["phase"] == "paused"
    assert all(row["reason"] == "appearance_unavailable"
               for row in qualification(events)[-1]["rows"])
    assert preview._reference_received_at is None


@pytest.mark.parametrize("control", ["horizontal", "vertical", "width", "height", "weak"])
def test_provably_incompatible_or_weak_competitor_can_remain_visible(control):
    preview = seed()
    for sequence, at in ((3, 1.4), (4, 1.5)):
        frame = pair(sequence, at, appearances=[A, None])
        other = frame["detections"][1]
        if control == "horizontal":
            other["box"][0] = .0
        elif control == "vertical":
            other["box"][1] = .36
        elif control == "width":
            other["box"][2] = .025
        elif control == "height":
            other["box"][3] = .2
        else:
            other["confidence"] = .499
        preview.observe(frame, at)
        assert preview.preview_detection_indices == [0, 1]
    assert preview.state(1.5)["phase"] == "tracking"


def test_ambiguity_clears_pending_and_two_subsequent_images_are_required():
    preview = seed()
    preview.observe(pair(), 1.4)
    assert preview._recovery_candidate[0] == 9
    preview.observe(pair(4, 1.45, appearances=[A, A]), 1.45)
    assert preview.state(1.45)["phase"] == "paused"
    assert preview._recovery_candidate is None
    preview.observe(pair(5, 1.5), 1.5)
    assert preview.state(1.5)["phase"] == "paused"
    preview.observe(pair(6, 1.6), 1.6)
    assert preview.state(1.6)["phase"] == "tracking"


def test_polls_repeated_images_and_equal_receipts_never_confirm_or_renew():
    events = []
    preview = seed(on_policy=events.append)
    frame = pair()
    preview.observe(frame, 1.4)
    estimate = preview._held_motion
    for at in (1.41, 1.42, 1.43):
        preview.observe(frame, at)
        assert preview.state(at)["phase"] == "paused"
    assert len(qualification(events)) == 1
    preview.observe(pair(4, 1.4), 1.44)
    assert preview.state(1.44)["phase"] == "paused"
    assert preview._last_seen_at == 1.2 and preview._held_motion is estimate
    preview.observe(pair(5, 1.5), 1.5)
    assert preview.state(1.5)["phase"] == "tracking"


def test_source_identity_validation_sees_every_candidate():
    preview = seed()
    frame = pair()
    preview.observe(frame, 1.4)
    changed = deepcopy(frame)
    changed["detections"][1]["confidence"] = .7
    preview.observe(changed, 1.41)
    assert preview.state(1.41)["phase"] == "stopped"


def test_pending_id_switch_requires_two_images_of_the_new_candidate():
    preview = seed()
    preview.observe(pair(), 1.4)
    preview.observe(pair(4, 1.5, ids=(10, 11)), 1.5)
    assert preview.state(1.5)["phase"] == "paused"
    preview.observe(pair(5, 1.6, ids=(10, 11)), 1.6)
    assert preview.state(1.6)["target_id"] == 10


def test_deadline_is_not_extended_by_ambiguous_images_and_terminal_stop_latches():
    preview = seed()
    for sequence in range(3, 33):
        at = 1.2 + (sequence - 2) * .1
        preview.observe(pair(sequence, at, center=.4, appearances=[A, A]), at)
        if at < 4.2:
            assert preview.state(at)["phase"] == "paused"
            assert preview.state(at)["recovery_deadline_at"] == 4.2
    assert preview.state(4.2)["phase"] == "stopped"
    preview.observe(observation(33, 4.3), 4.3)
    assert preview.state(4.3)["phase"] == "stopped"
    assert preview._held_motion is None


@pytest.mark.parametrize("action", ["clear", "reselect", "source", "clock", "stale"])
def test_lifecycle_fences_pending_motion_and_qualification(action):
    preview = seed()
    preview.observe(pair(), 1.4)
    epoch = preview.selection_epoch
    if action == "clear":
        preview.clear()
    elif action == "reselect":
        preview.select(11, revision=preview.revision, now=1.4)
        assert preview.state(1.4)["target_id"] == 11
    elif action == "source":
        frame = pair(4, 1.5)
        frame["video_id"] = "new-camera"
        preview.observe(frame, 1.5)
    elif action == "clock":
        preview.state(1.3)
    else:
        preview.state(1.851)
    assert preview._recovery_candidate is None
    assert preview._held_motion is None
    assert preview.selection_epoch > epoch
    assert preview._competition_latched is False


def test_competitor_disappearance_cannot_bypass_latched_same_id_confirmation():
    preview = seed()
    preview.observe(pair(3, 1.4, appearances=[A, A]), 1.4)
    assert preview._competition_latched is True
    frame = observation(4, 1.5, detections=[detection(7, .7)])
    preview.observe(frame, 1.5)
    assert preview.state(1.5)["phase"] == "paused"
    assert preview._competition_latched is True
    preview.observe(observation(5, 1.6, detections=[detection(7, .8)]), 1.6)
    assert preview.state(1.6)["phase"] == "tracking"
    assert preview._competition_latched is False


def test_competition_episode_discards_earlier_single_candidate_confirmation():
    preview = seed()
    preview.observe(observation(3, 1.3, detections=[detection(9, .5)]), 1.3)
    assert preview._recovery_candidate[0] == 9
    preview.observe(pair(4, 1.4), 1.4)
    assert preview.state(1.4)["phase"] == "paused"
    assert preview._recovery_candidate == (9, 4, 1.4)
    preview.observe(pair(5, 1.5), 1.5)
    assert preview.state(1.5)["phase"] == "tracking"


@pytest.mark.parametrize("appearance", [None, B, A])
def test_noncompetition_same_id_return_preserves_held_baseline(appearance):
    previews = [seed(policy=policy) for policy in (POLICY, "motion_held")]
    for preview in previews:
        preview.observe(observation(3, 1.3, detections=[], appearances=[]), 1.3)
        preview.observe(observation(4, 1.4, detections=[detection(7, .6)],
                                    appearances=[appearance]), 1.4)
    assert previews[0].state(1.4) == previews[1].state(1.4)
    assert previews[0]._competition_latched is False
    assert previews[0].state(1.4)["phase"] == "tracking"


def test_reference_expiry_during_poll_clears_pending_without_renewal():
    events = []
    preview = seed(on_recovery=events.append)
    for sequence in range(3, 15):
        at = 1.2 + (sequence - 2) * .2
        preview.observe(observation(sequence, at, detections=[detection(7, .4)],
                                    appearances=[None]), at)
    # Last selected receipt 3.6, descriptor still paired with receipt 1.0.
    frame = pair(15, 3.9, center=.4)
    preview.observe(frame, 3.9)
    assert preview._recovery_candidate == (9, 15, 3.9)
    preview.state(4.)
    assert preview._recovery_candidate is None
    assert preview.state(4.)["phase"] == "paused"
    assert preview._last_seen_at == pytest.approx(3.6)
    assert preview._reference_received_at == 1.
    preview.observe(pair(16, 4.1, center=.4), 4.1)
    assert preview.state(4.1)["phase"] == "paused"
    assert any(event["reason"] == "reference_appearance_expired" for event in events)


def test_active_strong_same_id_still_bypasses_identity_requalification():
    # A deliberately exposed limitation: an upstream reuse of the selected ID
    # can silently represent someone else. The recovery experiment cannot
    # establish identity while the existing tracker declares uninterrupted ID7.
    preview = seed()
    preview.observe(pair(3, 1.4, ids=(7, 11), appearances=[B, A]), 1.4)
    assert preview.state(1.4)["phase"] == "tracking"
    assert preview.state(1.4)["target_id"] == 7
    assert preview.preview_detection_indices == [0, 1]


def test_unknown_real_world_identity_is_not_an_input_to_the_policy():
    # With identical measurements an impostor is indistinguishable: two
    # qualifying observations can recover it. This is a counterexample, not a
    # successful person-identity claim or an annotation-controlled policy.
    preview = seed()
    for sequence, at in ((3, 1.4), (4, 1.5)):
        frame = pair(sequence, at, ids=(99, 11))
        frame["human_identity"] = "other_person"
        preview.observe(frame, at)
    assert preview.state(1.5)["phase"] == "tracking"
    assert preview.state(1.5)["target_id"] == 99


def test_diagnostic_callbacks_are_scalar_non_authoritative_and_no_boxes_are_filtered():
    events = []
    def callback(event):
        events.append(json.loads(json.dumps(event)))
        if event["event"] == "candidate_qualification":
            event["rows"].clear()
        raise RuntimeError("unavailable diagnostic sink")
    preview = seed(on_policy=callback, on_recovery=callback)
    for sequence, at in ((3, 1.4), (4, 1.5)):
        preview.observe(pair(sequence, at), at)
    assert preview.state(1.5)["phase"] == "tracking"
    for event in qualification(events):
        assert len(event["rows"]) == 2
        for row in event["rows"]:
            assert all(not isinstance(value, (dict, list, tuple)) for value in row.values())
    assert all(event.get("removed_detection_indices", []) == [] for event in events)
    assert all("appearance" not in item for item in preview.preview_detections)
    assert preview.metadata["offline_only"] is True
    assert preview.metadata["duplicate_filter"] is False


def test_dry_consumer_still_requires_its_own_fresh_confirmation():
    preview = seed()
    validator = YawValidator()
    def demand(at, *, explicit=False):
        state = preview.state(at)
        snapshot = dict(schema_version=1, at=at, run_id="run", environment="real",
            reconnecting=None,
            configuration=dict(environment="real", video_source="device", video_endpoint="/dev/video0"),
            video=dict(source_id="camera", source="device", state="recent", endpoint="/dev/video0",
                       received_at=state["frame_received_at"], rx_age_s=state["frame_age_s"], age_limit_s=1.),
            vision=dict(configured=True, state="recent", frame_age_s=state["frame_age_s"],
                        age_limit_s=1., inference_ms=1.), yaw_preview=state)
        return validator.validate(snapshot, at, at, explicit_selection=explicit)
    assert demand(1.2, explicit=True).valid
    preview.observe(pair(), 1.4)
    assert demand(1.4).reason == "target_paused"
    preview.observe(pair(4, 1.5), 1.5)
    assert demand(1.5).reason == "target_recovering"
    assert not demand(1.51).valid
    preview.observe(pair(5, 1.6), 1.6)
    assert demand(1.6).valid
