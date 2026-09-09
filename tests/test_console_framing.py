"""Framing lifecycle without transports, vehicle truth or a vision worker."""
from copy import deepcopy

import pytest

from argos.console.framing import AXES, DETECTION_PAUSE, NESTED_AMBIGUITY, FramingControl


CONTEXT = ("run-one", "video-one")


def zero():
    return dict.fromkeys(AXES, 0.)


def observation(at=10., sequence=1, *, context=CONTEXT, identity=7,
                box=None, confidence=.9, detections=None):
    return {"run_id": context[0], "video_id": context[1], "sequence": sequence,
            "received_at": at, "detections": detections if detections is not None else [
                {"track_id": identity, "confidence": confidence,
                 "box": list(box or (.4, .3, .1, .2))}]}


def nested_observation(at=10.3, sequence=3, *, target_box=(.4, .3, .1, .2),
                       target_confidence=.9, extra_box=None, extra_confidence=.4, **kwargs):
    x, y, width, height = target_box
    extra_box = list(extra_box or (x + .2 * width, y + .1 * height, .4 * width, .6 * height))
    return observation(at, sequence, detections=[
        {"track_id": 7, "box": list(target_box), "confidence": target_confidence},
        {"track_id": 8, "box": extra_box, "confidence": extra_confidence},
    ], **kwargs)


class Law:
    """Observable law boundary; numerical control behavior has separate tests."""

    def __init__(self):
        self.updates = []
        self.pauses = 0
        self.reset()

    def reset(self):
        self.reference_height = self.height = None
        self.error_x = self.error_y = 0.

    def start(self, box, received_at):
        self.reference_height = self.height = box[3]
        self.error_x = self.error_y = 0.

    def update(self, box, received_at):
        self.updates.append((list(box), received_at))
        self.height = box[3]
        self.error_x, self.error_y = .1, -.1
        return {"forward": .12, "right": 0., "up": -.1, "yaw": .2}

    def adjust(self, direction):
        self.reference_height *= 1.1 if direction == "closer" else 1 / 1.1

    def pause(self):
        self.pauses += 1


@pytest.fixture
def control():
    helper = FramingControl(enabled=True)
    helper._law = Law()
    return helper


def engaged(control):
    control.observe(observation())
    control.select(7, CONTEXT, 10.)
    control.engage(10.)
    return control


def moving(control):
    engaged(control)
    control.observe(observation(10.2, 2))
    control.tick(10.2)
    assert any(control.axes(10.2).values())
    return control


def test_disabled_is_passive_and_selection_needs_recent_matching_frame(control):
    disabled = FramingControl()
    disabled.observe(observation())
    with pytest.raises(RuntimeError, match="disabled"):
        disabled.select(7, CONTEXT, 10.)
    assert disabled.state(10.)["phase"] == "disabled"
    assert disabled.axes(10.) == zero()
    for frame, context, identity, now in [
        (None, CONTEXT, 7, 10.),
        (observation(), CONTEXT, 7, 10.451),
        (observation(), CONTEXT, 8, 10.),
        (observation(), ("another-run", CONTEXT[1]), 7, 10.),
        (observation(), CONTEXT, 7, 9.9),
    ]:
        control.observe(frame)
        with pytest.raises(RuntimeError, match="no longer present"):
            control.select(identity, context, now)
        assert control.phase == "idle"
        assert control.revision == 0


def test_selection_and_engagement_are_distinct_and_revision_fenced(control):
    control.observe(observation())
    control.select(7, CONTEXT, 10.)
    selected = control.state(10.)
    assert selected["phase"] == "selected" and selected["available"]
    assert selected["target_id"] == 7 and selected["revision"] == 1
    assert control.axes(10.) == zero()
    assert not control.state(10., vehicle_reason="AltHold required")["available"]
    control.engage(10.)
    active = control.state(10.)
    assert active["active"] and not active["available"]
    assert active["reference_height"] == .2 and active["revision"] == 2
    assert active["axes"] == zero()
    with pytest.raises(RuntimeError, match="Stop framing"):
        control.select(7, CONTEXT, 10.)
    with pytest.raises(RuntimeError):
        control.engage(10.)


@pytest.mark.parametrize("change,reason", [
    ({"confidence": .49}, "confidence"),
    ({"box": (.004, .3, .1, .2)}, "edge"),
    ({"box": (.9, .3, .096, .2)}, "edge"),
    ({"box": (.4, .004, .1, .2)}, "edge"),
    ({"box": (.4, .8, .1, .196)}, "edge"),
    ({"box": (.4, .3, .1, .079)}, "size"),
    ({"box": (.4, .3, .1, .451)}, "size"),
    ({"detections": [{"track_id": identity, "confidence": .9,
                      "box": [.4, .3, .1, .2]} for identity in (7, 8)]}, "exactly one"),
])
def test_engagement_refuses_unsuitable_selected_person(control, change, reason):
    control.observe(observation(**change))
    control.select(7, CONTEXT, 10.)
    with pytest.raises(RuntimeError, match=reason):
        control.engage(10.)
    assert not control.state(10.)["available"]
    assert control.phase == "selected" and control.axes(10.) == zero()


@pytest.mark.parametrize("height", [.08, .45])
def test_engagement_height_boundaries_are_inclusive(control, height):
    control.observe(observation(box=(.4, .2, .1, height)))
    control.select(7, CONTEXT, 10.)
    control.engage(10.)
    assert control.state(10.)["reference_height"] == height


def test_repeated_frame_and_equal_receipt_time_never_advance_law(control):
    engaged(control)
    control.tick(10.1)
    control.tick(10.2)
    control.observe(observation(10., 2))
    control.tick(10.3)
    assert control._law.updates == []
    # Snapshot expiry already suppresses output, without changing the lifecycle.
    assert control.axes(10.451) == zero()
    assert control.phase == "active"
    control.tick(10.451)
    assert control.phase == "takeover"
    assert "stale" in control.state(10.451)["reason"]


@pytest.mark.parametrize("frame", [observation(10.3, 2),
    observation(10.2, 2, box=(.5, .3, .1, .2))])
def test_repeated_sequence_cannot_replace_metadata_or_extend_command_freshness(control, frame):
    moving(control)
    control.observe(frame)
    assert control.axes(10.3) == zero()
    control.tick(10.3)
    assert control.phase == "takeover"
    assert "identity changed" in control.state(10.3)["reason"]
    assert len(control._law.updates) == 1


def test_each_distinct_frame_updates_once_and_outputs_are_copied(control):
    moving(control)
    control.tick(10.21)
    control.tick(10.3)
    assert len(control._law.updates) == 1
    returned = control.axes(10.3)
    returned["yaw"] = 1.
    assert control.axes(10.3)["yaw"] == .2
    assert control.axes(10.651) == zero()
    assert control.phase == "active"  # axes() is a pure safety guard.


@pytest.mark.parametrize("frame,reason", [
    (None, "No recent"),
    (observation(10.3, 3, identity=8), "lost"),
    (observation(10.3, 3, context=("run-two", "video-one")), "source changed"),
    (observation(10.3, 3, context=("run-one", "video-two")), "source changed"),
    (observation(10.3, 3, box=(.001, .3, .1, .2)), "edge"),
    (observation(10.3, 3, confidence=.49, box=(.001, .3, .1, .2)), "edge"),
    (observation(10.3, 3, box=(.4, .3, .1, .059)), "size"),
    (observation(10.3, 3, confidence=.49, box=(.4, .3, .1, .059)), "size"),
    (observation(10.3, 3, box=(.4, .1, .1, .651)), "size"),
    (observation(10.3, 3, detections=[{"track_id": i, "confidence": .9,
                  "box": [.4, .3, .1, .2]} for i in (7, 8)]), "exactly one"),
    (observation(10.3, 1), "order changed"),
    (observation(10.1, 3), "order changed"),
])
def test_loss_neutralizes_and_latches_takeover(control, frame, reason):
    moving(control)
    control.observe(deepcopy(frame))
    control.tick(10.3)
    state = control.state(10.3)
    assert state["phase"] == "takeover" and not state["available"]
    assert reason in state["reason"]
    assert state["axes"] == zero() and state["reference_height"] is None
    assert state["takeover_remaining_s"] == 2.
    assert state["target_id"] == 7
    assert not control.takeover_due(12.299)
    assert control.takeover_due(12.3)


@pytest.mark.parametrize("change", [{"detections": []}, {"confidence": .49}])
def test_isolated_detection_miss_pauses_with_zero_axes_until_same_id_returns(control, change):
    moving(control)
    reference = control.state(10.2)["reference_height"]
    control.observe(observation(10.3, 3, **change))
    assert control.axes(10.3) == zero()  # immediate guard, before the next tick
    control.tick(10.3)
    paused = control.state(10.3)
    assert paused["active"] and paused["paused"] and not paused["available"]
    assert "paused" in paused["reason"]
    assert paused["height"] is paused["error_x"] is paused["error_y"] is None
    assert paused["reference_height"] == reference
    assert paused["axes"] == zero() and paused["takeover_remaining_s"] is None
    assert control._law.pauses == 1 and len(control._law.updates) == 1
    # Fresh low/empty results may bridge the observed 400 ms detector gap,
    # without advancing the law or reusing a command while perception is bad.
    control.observe(observation(10.5, 4, detections=[]))
    control.tick(10.5)
    control.tick(10.699)
    assert control.state(10.699)["paused"] and control.axes(10.699) == zero()
    assert len(control._law.updates) == 1
    control.observe(observation(10.7, 5, box=(.45, .3, .1, .19)))
    assert control.axes(10.7) == zero()  # an observation cannot restore old output
    control.tick(10.7)
    recovered = control.state(10.7)
    assert recovered["active"] and not recovered["paused"]
    assert recovered["reference_height"] == reference
    assert recovered["height"] == .19 and recovered["target_id"] == 7
    assert control._law.updates[-1] == ([.45, .3, .1, .19], 10.7)
    assert len(control._law.updates) == 2


@pytest.mark.parametrize("change", [{"detections": []}, {"confidence": .49}])
def test_six_hundred_ms_pause_does_not_extend_and_expiry_wins_over_a_good_image(control, change):
    moving(control)
    control.observe(observation(10.25, 3, **change))
    control.tick(10.3)  # deadline starts at processing, not the image receipt
    revision = control.revision
    deadline = 10.3 + .6  # explicit chosen policy, independent of the implementation constant
    for sequence, at in enumerate((10.4, 10.5, 10.6, 10.7, 10.8, deadline - .001), 4):
        control.observe(observation(at, sequence, **change))
        control.tick(at)
        assert control.state(at)["paused"] and control.axes(at) == zero()
        with pytest.raises(RuntimeError, match="paused"):
            control.adjust("closer", at)
        assert control.revision == revision
        assert control.state(at)["reference_height"] == .2
    assert control._law.pauses == 1
    control.observe(observation(deadline, sequence + 1))
    control.tick(deadline)
    state = control.state(deadline)
    assert state["phase"] == "takeover" and not state["paused"]
    assert state["reference_height"] is None and state["axes"] == zero()
    assert state["takeover_remaining_s"] == 2.
    assert len(control._law.updates) == 1
    control.observe(observation(deadline + .1, sequence + 2))
    control.tick(deadline + .1)
    assert control.phase == "takeover" and len(control._law.updates) == 1
    assert control.takeover_due(deadline + 2.)


def test_frozen_image_stales_before_the_longer_pause_can_resume(control):
    moving(control)
    control.observe(observation(10.3, 3, detections=[]))
    control.tick(10.3)
    control.tick(10.749)
    assert control.state(10.749)["paused"]
    control.tick(10.751)
    assert control.phase == "takeover"
    assert "stale" in control.state(10.751)["reason"]
    assert control.state(10.751)["takeover_remaining_s"] == 2.
    # Even a good frame inside the 600 ms pause budget cannot undo staleness.
    control.observe(observation(10.8, 4))
    control.tick(10.8)
    assert control.phase == "takeover" and control.axes(10.8) == zero()


@pytest.mark.parametrize("frame,reason", [
    (None, "No recent"),
    (observation(10.4, 4, identity=8), "lost"),
    (observation(10.4, 4, context=("other-run", "video-one")), "source changed"),
    (observation(10.4, 4, confidence=.3, box=(.001, .3, .1, .2)), "edge"),
    (observation(10.4, 4, detections=[{"track_id": i, "confidence": .9,
                  "box": [.4, .3, .1, .2]} for i in (7, 8)]), "exactly one"),
    (observation(10.2, 2), "order changed"),
])
def test_other_failures_during_pause_latch_immediately(control, frame, reason):
    moving(control)
    control.observe(observation(10.3, 3, detections=[]))
    control.tick(10.3)
    control.observe(frame)
    control.tick(10.4)
    assert control.phase == "takeover" and not control.state(10.4)["paused"]
    assert reason in control.state(10.4)["reason"]
    assert control.axes(10.4) == zero()


def test_vehicle_or_stale_image_during_pause_cannot_resume(control):
    moving(control)
    control.observe(observation(10.21, 3, detections=[]))
    control.tick(10.6)  # a fresh image may already be close to its .45s age limit
    control.observe(observation(10.22, 4))
    control.tick(10.7)
    assert control.phase == "takeover" and "stale" in control.state(10.7)["reason"]
    control.clear()
    moving(control)
    control.observe(observation(10.3, 3, detections=[]))
    control.tick(10.3)
    control.observe(observation(10.4, 4))
    control.tick(10.4, vehicle_reason="Airborne state is unavailable")
    assert control.phase == "takeover"
    assert control.state(10.4)["reason"] == "Airborne state is unavailable"


def test_repeated_receipt_and_snapshot_reads_cannot_resume_or_extend_pause(control):
    moving(control)
    control.observe(observation(10.3, 3, detections=[]))
    control.tick(10.3)
    control.observe(observation(10.3, 4))
    control.tick(10.4)
    assert control.state(10.4)["paused"] and control.axes(10.4) == zero()
    revision = control.revision
    for at in (10.4, 10.5, 50.):
        assert control.state(at)["paused"]
        assert control.axes(at) == zero()
    assert control.revision == revision and len(control._law.updates) == 1
    control.tick(10.3 + DETECTION_PAUSE)
    assert control.phase == "takeover"


@pytest.mark.parametrize("operation", ["stop", "clear"])
def test_explicit_manual_stop_or_clear_removes_pause_and_reference(control, operation):
    moving(control)
    control.observe(observation(10.3, 3, detections=[]))
    control.tick(10.3)
    getattr(control, operation)()
    assert not control.state(10.3)["paused"]
    assert control.state(10.3)["reference_height"] is None
    control.observe(observation(10.4, 4))
    control.select(7, CONTEXT, 10.4)
    control.engage(10.4)
    control.observe(observation(10.7, 5))
    control.tick(10.7)
    assert control.phase == "active" and not control.state(10.7)["paused"]


def test_pause_failure_latches_takeover(control):
    moving(control)
    def fail():
        raise RuntimeError("pause failed")
    control._law.pause = fail
    control.observe(observation(10.3, 3, detections=[]))
    control.tick(10.3)
    assert control.phase == "takeover" and control.axes(10.3) == zero()
    assert "pause failed" in control.state(10.3)["reason"]


def test_recovery_ticks_reads_and_adjustments_cannot_reset_takeover_deadline(control):
    moving(control)
    control.observe(None)
    control.tick(10.3)
    failed = control.state(10.3)
    control.observe(observation(11., 10))
    for at in (11., 11.1, 11.2):
        control.tick(at)
        with pytest.raises(RuntimeError):
            control.adjust("closer", at)
        with pytest.raises(RuntimeError):
            control.engage(at)
        with pytest.raises(RuntimeError):
            control.select(7, CONTEXT, at)
    state = control.state(11.2, vehicle_reason="another later failure")
    assert state["phase"] == "takeover" and state["reason"] == failed["reason"]
    assert state["revision"] == failed["revision"]
    assert state["takeover_remaining_s"] == pytest.approx(1.1)
    assert control.takeover_due(12.3)
    assert control.axes(11.2) == zero()


def test_vehicle_problem_uses_same_latched_neutral_handoff(control):
    moving(control)
    control.tick(10.3, vehicle_reason="Airborne state is unavailable")
    assert control.phase == "takeover"
    assert control.state(10.3)["reason"] == "Airborne state is unavailable"
    assert control.axes(10.3) == zero()


def test_explicit_stop_acknowledges_loss_without_automatic_reengagement(control):
    moving(control)
    control.observe(None)
    control.tick(10.3)
    control.observe(observation(10.5, 4))
    control.tick(10.5)
    revision = control.revision
    control.stop()
    assert control.phase == "selected" and control.revision == revision + 1
    assert control.state(10.5)["available"] and control.state(10.5)["reference_height"] is None
    assert control.axes(10.5) == zero() and not control.takeover_due(50.)
    control.tick(10.6)
    assert control.phase == "selected"
    control.engage(10.6)
    assert control.phase == "active"


def test_stop_clears_invalid_selection_and_even_idle_stop_invalidates_pending_requests(control):
    moving(control)
    control.observe(observation(10.3, 3, identity=8))
    control.tick(10.3)
    control.stop()
    assert control.phase == "idle" and control.state(10.3)["target_id"] is None
    revision = control.revision
    control.stop()
    assert control.revision == revision + 1
    assert not control.state(10.3)["available"]


def test_clear_invalidates_target_reference_output_and_deadline(control):
    moving(control)
    control.tick(10.3, vehicle_reason="Need takeover")
    revision = control.revision
    control.clear("Flight ended")
    state = control.state(10.3)
    assert state["phase"] == "idle" and state["revision"] == revision + 1
    assert state["target_id"] is None and state["reference_height"] is None
    assert state["axes"] == zero() and state["takeover_remaining_s"] is None
    assert state["reason"] == "Flight ended"


def test_size_adjustment_is_active_only_and_never_conceals_stale_input(control):
    with pytest.raises(RuntimeError):
        control.adjust("closer", 10.)
    moving(control)
    before = control.revision
    control.adjust("closer", 10.25)
    assert control.state(10.25)["reference_height"] == pytest.approx(.22)
    assert control.revision == before + 1
    control.adjust("farther", 10.3)
    assert control.state(10.3)["reference_height"] == pytest.approx(.2)
    with pytest.raises(ValueError):
        control.adjust("metres", 10.35)
    with pytest.raises(RuntimeError):
        control.adjust("closer", 10.7)
    assert control.phase == "takeover" and control.axes(10.7) == zero()
    assert control.state(10.7)["takeover_remaining_s"] == 2.


@pytest.mark.parametrize("outcome", [RuntimeError("law failed"), {"forward": .2},
    {"forward": float("nan"), "right": 0., "up": 0., "yaw": 0.},
    {"forward": 2., "right": 0., "up": 0., "yaw": 0.}])
def test_law_failure_or_invalid_output_neutralizes(control, outcome):
    engaged(control)
    def fail(*args):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    control._law.update = fail
    control.observe(observation(10.2, 2))
    control.tick(10.2)
    assert control.phase == "takeover" and control.axes(10.2) == zero()
    assert "calculation failed" in control.state(10.2)["reason"]


@pytest.mark.parametrize("axis,value", [
    ("forward", .350001), ("forward", -.350001),
    ("up", .300001), ("up", -.300001),
    ("yaw", .500001), ("yaw", -.500001),
    ("right", .000001), ("right", -.000001),
])
def test_guidance_cannot_expand_framing_authority(control, axis, value):
    moving(control)
    output = zero()
    output[axis] = value
    control._law.update = lambda *args: output
    control.observe(observation(10.3, 3))
    control.tick(10.3)
    assert control.phase == "takeover"
    assert control.axes(10.3) == zero()
    assert control.takeover_due(12.3)


@pytest.mark.parametrize("sign", [-1, 1])
def test_guidance_authority_boundaries_are_inclusive(control, sign):
    engaged(control)
    output = {"forward": sign * .35, "right": 0., "up": sign * .3, "yaw": sign * .5}
    control._law.update = lambda *args: output
    control.observe(observation(10.2, 2))
    control.tick(10.2)
    assert control.phase == "active"
    assert control.axes(10.2) == output


def test_bad_metadata_becomes_unavailable_without_escaping_observe(control):
    moving(control)
    control.observe({"unexpected": "metadata"})
    control.tick(10.3)
    assert control.phase == "takeover"
    assert "Invalid analyzed" in control.state(10.3)["reason"]


def test_observation_and_snapshot_mutation_cannot_change_selected_target(control):
    frame = observation()
    control.observe(frame)
    frame["detections"][0]["track_id"] = 999
    frame["detections"][0]["box"][3] = .99
    control.select(7, CONTEXT, 10.)
    control.engage(10.)
    state = control.state(10.)
    state["axes"]["forward"] = 1.
    assert control.state(10.)["reference_height"] == .2
    assert control.axes(10.) == zero()


def test_state_read_is_pure_and_cannot_extend_freshness_or_deadlines(control):
    moving(control)
    revision = control.revision
    for at in (10.3, 10.4, 10.7, 20.):
        control.state(at)
        control.axes(at)
        control.takeover_due(at)
    assert control.revision == revision and control.phase == "active"
    assert len(control._law.updates) == 1
    control.tick(20.)
    assert control.phase == "takeover" and control.takeover_due(22.)


def test_real_law_lifecycle_keeps_reference_until_explicit_manual_stop():
    helper = FramingControl(enabled=True)
    helper.observe(observation(box=(.45, .4, .1, .2)))
    helper.select(7, CONTEXT, 10.)
    helper.engage(10.)
    assert helper.axes(10.) == zero()
    helper.observe(observation(10.2, 2, box=(.455, .41, .09, .18)))
    helper.tick(10.2)
    assert helper.axes(10.2)["forward"] > 0
    assert helper.state(10.2)["reference_height"] == .2
    helper.adjust("closer", 10.25)
    assert helper.state(10.25)["reference_height"] == pytest.approx(.22)
    helper.stop()
    assert helper.phase == "selected" and helper.axes(10.25) == zero()
    assert helper.state(10.25)["reference_height"] is None


@pytest.mark.parametrize("first,second,reason", [
    ({"detections": []}, {"confidence": .4}, "No person detection"),
    ({"confidence": .4}, {"detections": []}, "confidence is too low"),
])
def test_pause_expiry_retains_original_failure_image_not_late_recovery(control, first, second, reason):
    moving(control)
    bad_frame = observation(10.25, 3, **first)
    control.observe(bad_frame)
    control.tick(10.3)
    assert control.last_loss is None  # a pause alone is not an actual takeover
    control.observe(observation(10.4, 4, **second))
    control.tick(10.4)
    control.observe(observation(10.6, 5, **second))
    control.tick(10.6)
    deadline = 10.3 + DETECTION_PAUSE
    control.observe(observation(deadline - .001, 6))
    control.tick(deadline - .001)
    assert control.phase == "active" and control.last_loss is None
    # Start a second pause and expire it with a good frame arriving exactly at
    # its deadline. Only this actual takeover creates retained loss evidence.
    second_started = deadline + .1
    bad_frame = observation(second_started - .05, 7, **first)
    control.observe(bad_frame)
    control.tick(second_started)
    control.observe(observation(second_started + .2, 8, **second))
    control.tick(second_started + .2)
    deadline = second_started + DETECTION_PAUSE
    control.observe(observation(deadline, 9))
    control.tick(deadline)
    loss = control.state(deadline)["last_loss"]
    assert loss["at"] == deadline and loss["evidence_at"] == second_started
    assert loss["sequence"] == 7 and loss["target_id"] == 7
    assert loss["frame_age_s"] == pytest.approx(.05)
    assert loss["detections"] == bad_frame["detections"]
    assert reason in loss["reason"] and "framing pause" in loss["reason"]
    assert not control.takeover_due(deadline + 1.999)
    assert control.takeover_due(deadline + 2.)


@pytest.mark.parametrize("frame,now,reason", [
    (observation(10.3, 3, identity=8), 10.3, "different person IDs"),
    (observation(10.3, 3), 10.751, "stale"),
    (None, 10.3, "No recent analyzed image"),
])
def test_immediate_loss_retains_actual_takeover_metadata(control, frame, now, reason):
    moving(control)
    control.observe(frame)
    control.tick(now)
    loss = control.last_loss
    assert loss["at"] == loss["evidence_at"] == now
    assert reason in loss["reason"]
    assert loss["target_id"] == 7
    assert loss["sequence"] == (None if frame is None else frame["sequence"])
    assert loss["detections"] == ([] if frame is None else frame["detections"])
    assert loss["frame_age_s"] == (None if frame is None else pytest.approx(now - frame["received_at"]))


def test_retained_loss_survives_clear_and_later_observations_with_defensive_copies(control):
    moving(control)
    control.observe(observation(10.3, 3, identity=8))
    control.tick(10.3)
    original = control.last_loss
    returned = control.state(10.3)["last_loss"]
    returned["reason"] = "changed by a consumer"
    returned["detections"][0]["confidence"] = 0.
    returned["detections"][0]["box"][0] = .99
    control.last_loss["detections"].clear()
    control.observe(observation(11., 4))
    control.tick(11.)
    control.clear("Drone disarmed; framing stopped")
    control.stop()
    assert control.state(50.)["last_loss"] == original
    assert control.state(50.)["target_id"] is None
    with pytest.raises(RuntimeError):
        control.select(7, CONTEXT, 50.)
    assert control.last_loss == original  # rejected intent cannot erase evidence


@pytest.mark.parametrize("operation", ["select", "engage", "new_lease"])
def test_successful_new_operator_intent_resets_retained_loss(control, operation):
    moving(control)
    control.tick(10.3, vehicle_reason="Vehicle state unavailable")
    control.stop()
    assert control.last_loss is not None
    if operation == "select":
        control.select(7, CONTEXT, 10.3)
    elif operation == "engage":
        control.engage(10.3)
    else:
        control.clear("New control lease", reset_loss=True)
    assert control.last_loss is None


def test_retained_loss_has_bounded_metadata_without_raw_image_fields(control):
    moving(control)
    detections = [{"track_id": i, "confidence": .9, "box": [.4, .3, .1, .2]}
                  for i in range(1, 17)]
    frame = observation(10.3, 3, detections=detections)
    frame["raw_pixels"] = b"not diagnostic data"
    control.observe(frame)
    control.tick(10.3, vehicle_reason="x" * 5000)
    loss = control.last_loss
    assert len(loss["detections"]) == 16
    assert len(loss["reason"]) == 1024
    assert "raw_pixels" not in loss


def test_nested_weak_ambiguity_keeps_both_boxes_and_needs_two_fresh_clean_images(control):
    moving(control)
    frame = nested_observation()
    control.observe(frame)
    assert control.axes(10.3) == zero()
    control.tick(10.3)
    assert control.state(10.3)["paused"] and NESTED_AMBIGUITY in control.state(10.3)["reason"]
    assert control._observation["detections"] == frame["detections"]
    assert control._law.pauses == 1 and len(control._law.updates) == 1
    reference, deadline = control._law.reference_height, control._pause_deadline
    control.observe(observation(10.5, 4))
    control.tick(10.5)
    assert control.state(10.5)["paused"] and "second fresh" in control.state(10.5)["reason"]
    for now in (10.51, 10.6):
        control.tick(now)
        control.state(now)
        assert control.axes(now) == zero()
    control.observe(observation(10.5, 5))  # Different sequence with the same receipt is not confirmation.
    control.tick(10.6)
    assert control._pause_clean_count == 1 and len(control._law.updates) == 1
    assert control._pause_deadline == deadline
    control.observe(observation(10.7, 6, box=(.45, .3, .1, .19)))
    control.tick(10.7)
    assert control.phase == "active" and not control.state(10.7)["paused"]
    assert control._law.reference_height == reference
    assert control._law.updates[-1] == ([.45, .3, .1, .19], 10.7)
    assert len(control._law.updates) == 2 and not control._pause_ambiguity


def test_nested_ambiguity_never_allows_initial_engagement(control):
    control.observe(nested_observation(10., 1))
    control.select(7, CONTEXT, 10.)
    with pytest.raises(RuntimeError, match="weak nested detection ambiguity"):
        control.engage(10.)
    assert control.phase == "selected" and not control.state(10.)["available"]
    assert not control.state(10.)["paused"] and control.axes(10.) == zero()


@pytest.mark.parametrize("change,reason", [
    ({"target_box": (.001, .3, .1, .2)}, "edge"),
    ({"target_box": (.4, .3, .1, .059)}, "size"),
    ({"target_box": (.4, .1, .1, .651)}, "size"),
    ({"context": ("another-run", "video-one")}, "source changed"),
    ({"at": 9.8}, "stale"),
    ({"at": 10.1}, "order changed"),
    ({"sequence": 1}, "order changed"),
    ({"target_confidence": .49}, "exactly one"),
    ({"extra_confidence": .5}, "exactly one"),
    ({"extra_confidence": .9}, "exactly one"),
    ({"extra_box": (.7, .3, .05, .1)}, "exactly one"),
    ({"extra_box": (.39, .32, .04, .12)}, "exactly one"),
])
def test_nested_shape_cannot_mask_other_loss_conditions(control, change, reason):
    moving(control)
    control.observe(nested_observation(**change))
    control.tick(10.3)
    assert control.phase == "takeover" and reason in control.state(10.3)["reason"]
    assert control.axes(10.3) == zero() and not control.state(10.3)["paused"]


def test_three_nested_detections_or_missing_selected_id_latch(control):
    for missing in (False, True):
        control.clear()
        moving(control)
        frame = nested_observation()
        if missing:
            frame["detections"][0]["track_id"] = 9
        else:
            frame["detections"].append({**frame["detections"][1], "track_id": 9})
        control.observe(frame)
        control.tick(10.3)
        assert control.phase == "takeover" and control.axes(10.3) == zero()


@pytest.mark.parametrize("middle", ["nested", "empty", "weak"])
def test_bad_image_resets_clean_confirmation_without_extending_ambiguity_budget(control, middle):
    moving(control)
    control.observe(nested_observation())
    control.tick(10.3)
    deadline = control._pause_deadline
    control.observe(observation(10.4, 4))
    control.tick(10.4)
    bad = (nested_observation(10.5, 5) if middle == "nested" else
           observation(10.5, 5, detections=[]) if middle == "empty" else
           observation(10.5, 5, confidence=.4))
    control.observe(bad)
    control.tick(10.5)
    assert control._pause_clean_count == 0 and control._pause_ambiguity
    assert control._pause_deadline == deadline and control._law.pauses == 1
    control.observe(observation(10.6, 6))
    control.tick(10.6)
    assert control.state(10.6)["paused"] and control.axes(10.6) == zero()
    control.observe(observation(10.8, 7))
    control.tick(10.8)
    assert not control.state(10.8)["paused"] and len(control._law.updates) == 2


def test_ordinary_pause_upgrades_to_two_frame_confirmation_without_new_time(control):
    moving(control)
    first_bad = observation(10.3, 3, detections=[])
    control.observe(first_bad)
    control.tick(10.3)
    deadline = control._pause_deadline
    control.observe(nested_observation(10.5, 4))
    control.tick(10.5)
    control.observe(observation(10.7, 5))
    control.tick(10.7)
    assert control.state(10.7)["paused"] and control._pause_deadline == deadline
    control.observe(observation(deadline, 6))
    control.tick(deadline)
    assert control.phase == "takeover" and control.axes(deadline) == zero()
    assert control.last_loss["detections"] == first_bad["detections"]
    assert control.last_loss["sequence"] == 3 and "No person detection" in control.last_loss["reason"]


def test_second_clean_image_at_deadline_loses_and_preserves_both_original_boxes(control):
    moving(control)
    first_bad = nested_observation()
    control.observe(first_bad)
    control.tick(10.3)
    deadline = control._pause_deadline
    control.observe(observation(10.7, 4))
    control.tick(10.7)
    control.observe(observation(deadline, 5))
    control.tick(deadline)
    assert control.phase == "takeover" and len(control._law.updates) == 1
    assert control.last_loss["at"] == deadline and control.last_loss["sequence"] == 3
    assert control.last_loss["detections"] == first_bad["detections"]
    assert NESTED_AMBIGUITY in control.last_loss["reason"]
    assert not control.takeover_due(deadline + 1.999) and control.takeover_due(deadline + 2.)
    assert not control._pause_ambiguity and control._pause_clean_count == 0


@pytest.mark.parametrize("failure,reason", [
    (observation(10.6, 5, identity=8), "different person IDs"),
    (observation(10.6, 5, context=("other", "video-one")), "source changed"),
    (nested_observation(10.6, 5, extra_confidence=.9), "exactly one"),
    (nested_observation(10.6, 5, extra_box=(.7, .3, .05, .1)), "exactly one"),
    (nested_observation(10.6, 5, target_box=(.001, .3, .1, .2)), "edge"),
    (observation(10.3, 5), "order changed"),
])
def test_unsafe_second_confirmation_latches_immediately(control, failure, reason):
    moving(control)
    control.observe(nested_observation())
    control.tick(10.3)
    control.observe(observation(10.4, 4))
    control.tick(10.4)
    control.observe(failure)
    control.tick(10.6)
    assert control.phase == "takeover" and reason in control.state(10.6)["reason"]
    assert len(control._law.updates) == 1 and control.axes(10.6) == zero()


@pytest.mark.parametrize("vehicle_failure", [False, True])
def test_first_confirmation_can_stale_or_lose_vehicle_before_second(control, vehicle_failure):
    moving(control)
    control.observe(nested_observation())
    control.tick(10.3)
    control.observe(observation(10.4, 4))
    control.tick(10.4)
    control.tick(10.851, vehicle_reason="Vehicle unavailable" if vehicle_failure else "")
    assert control.phase == "takeover" and len(control._law.updates) == 1
    assert ("Vehicle unavailable" if vehicle_failure else "stale") in control.state(10.851)["reason"]


def test_persistently_contained_second_person_keeps_both_measurements_and_no_commands(control):
    # The geometry could represent two real overlapping people. It authorizes
    # waiting with zero commands, never suppressing the weaker measurement.
    moving(control)
    for sequence, at in enumerate((10.3, 10.4, 10.6, 10.8), 3):
        frame = nested_observation(at, sequence, target_confidence=.5, extra_confidence=.499)
        frame["detections"].reverse()  # List order conveys no preferred identity.
        control.observe(frame)
        control.tick(at)
        assert control.state(at)["paused"] and control.axes(at) == zero()
        assert control._observation["detections"] == frame["detections"]
    deadline = control._pause_deadline
    control.observe(nested_observation(deadline, 7))
    control.tick(deadline)
    assert control.phase == "takeover" and len(control._law.updates) == 1
    assert len(control.last_loss["detections"]) == 2


@pytest.mark.parametrize("operation", ["stop", "clear"])
def test_manual_acknowledgement_clears_ambiguity_confirmation(control, operation):
    moving(control)
    control.observe(nested_observation())
    control.tick(10.3)
    control.observe(observation(10.4, 4))
    control.tick(10.4)
    getattr(control, operation)()
    assert not control._pause_ambiguity and control._pause_clean_count == 0
    assert control._pause_deadline is None and control.axes(10.4) == zero()
    control.observe(observation(10.5, 5))
    control.tick(10.5)
    assert control.phase != "active"
    control.select(7, CONTEXT, 10.5)
    control.engage(10.5)
    control.observe(observation(10.6, 6, detections=[]))
    control.tick(10.6)
    control.observe(observation(10.7, 7))
    control.tick(10.7)
    assert control.phase == "active" and not control.state(10.7)["paused"]
