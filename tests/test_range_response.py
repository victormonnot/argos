"""Distance-response choices preserve framing authority and bounded image control.

Observation sequences verify requested axes; they do not predict flight speed.
The HTTP cases use the production routing/control path with injected receipts.
"""
from copy import deepcopy
import math

import pytest

from argos.console.framing import DETECTION_PAUSE, FramingControl
from argos.guidance.image_framing import AXIS_LIMITS, RANGE_RESPONSES, FramingLaw
from test_console_framing import CONTEXT, observation
from test_console_flight_control import heartbeat, landed, simstate
from test_console_mode_switch import hover, thrust
from test_console_pilot_throttle_api import PERSON, pilot_flight


def box(height=.2, cx=.5, cy=.5):
    return [cx - .05, cy - height / 2, .1, height]


def law_for(response="normal", *, vertical_control=True):
    law = FramingLaw()
    law.start(box(), 1., range_response=response, vertical_control=vertical_control)
    return law


def test_normal_default_matches_explicit_normal_and_pre_preset_trace():
    # Captured from the previous law, including braking, pause, reference change
    # and immediate alignment inhibition. Tolerance covers platform libm rounding.
    expected_forward = [
        .06999999999999998, .017603153347964265, -.0523968466520358,
        -.12239684665203578, .054557093907672, .02119853097615826,
        0., .02119853097615826,
    ]
    traces = []
    for start_fields in ({}, {"range_response": "normal"}):
        law = FramingLaw()
        law.start(box(.18), 1., **start_fields)
        for _ in range(3):
            law.adjust("closer")
        trace = [law.update(box(height, .53, .55), 1. + step * .2)
                 for step, height in enumerate((.182, .19, .20, .21), 1)]
        law.pause()
        trace.append(law.update(box(.205, .53, .55), 2.))
        law.adjust("farther")
        trace.extend((law.update(box(.205), 2.2),
                      law.update(box(.205, .64), 2.21),
                      law.update(box(.205), 2.4)))
        traces.append(trace)
    assert traces[0] == traces[1]
    assert [axes["forward"] for axes in traces[0]] == pytest.approx(expected_forward, abs=1e-12)
    for index, axes in enumerate(traces[0]):
        assert axes["right"] == 0.
        assert axes["up"] == pytest.approx(-.016 if index < 5 else 0., abs=1e-12)
        assert axes["yaw"] == pytest.approx(.025 if index < 5 else .009 if index == 6 else 0., abs=1e-12)


@pytest.mark.parametrize("direction,sign", [("closer", 1), ("farther", -1)])
def test_presets_change_response_magnitude_without_changing_reference_step(direction, sign):
    requests, references = [], []
    for response, scale in RANGE_RESPONSES.items():
        law = law_for(response)
        references.append(law.adjust(direction))
        requested = law.update(box(), 1.2)["forward"]
        assert requested == pytest.approx(sign * .35 * math.log(1.1) * scale)
        requests.append(abs(requested))
    assert references == [references[0]] * 3
    assert 0 < requests[0] < requests[1] < requests[2]


@pytest.mark.parametrize("response,scale", list(RANGE_RESPONSES.items()))
def test_presets_scale_braking_as_well_as_approach(response, scale):
    law = FramingLaw()
    law.start(box(.18), 1., range_response=response)
    for _ in range(3):
        law.adjust("closer")
    for step, height in enumerate((.182, .19, .20, .21), 1):
        axes = law.update(box(height), 1 + step * .2)
    assert law.height < law.reference_height
    assert axes["forward"] == pytest.approx(-.12239684665203578 * scale, abs=1e-12)


@pytest.mark.parametrize("vertical_control", [False, True])
def test_all_presets_keep_identical_centering_and_existing_axis_limits(vertical_control):
    laws = [law_for(response, vertical_control=vertical_control) for response in RANGE_RESPONSES]
    for law in laws:
        for _ in range(50):
            law.adjust("closer")
        assert law.reference_height == .45
    for step in range(1, 31):
        sample = box(.08 if step < 15 else .6, .53 if step < 20 else .8, .55)
        outputs = [law.update(sample, 1 + step * .2) for law in laws]
        for axis in ("right", "up", "yaw"):
            assert len({axes[axis] for axes in outputs}) == 1
        for axes in outputs:
            assert all(abs(axes[axis]) <= limit + 1e-12 for axis, limit in AXIS_LIMITS.items())
            if not vertical_control:
                assert axes["up"] == 0.


@pytest.mark.parametrize("response", list(RANGE_RESPONSES))
@pytest.mark.parametrize("changed", [box(cx=.63), box(cy=.63), [0., .4, 1., .2]])
def test_all_presets_inhibit_forward_immediately_on_lost_alignment_or_clipping(response, changed):
    law = law_for(response)
    law.adjust("closer")
    assert law.update(box(), 1.2)["forward"] > 0.
    assert law.update(changed, 1.200001)["forward"] == 0.


@pytest.mark.parametrize("response,scale", list(RANGE_RESPONSES.items()))
def test_switch_keeps_history_and_applies_new_slew_only_on_next_image(response, scale):
    law = law_for("responsive")
    for _ in range(20):
        law.adjust("closer")
    law.update(box(.205, .53, .55), 1.2)
    previous = deepcopy(law.__dict__)
    law.set_range_response(response)
    assert {k: v for k, v in law.__dict__.items() if k != "range_response"} == {
        k: v for k, v in previous.items() if k != "range_response"}
    with pytest.raises(ValueError, match="strictly increase"):
        law.update(box(), 1.2)
    axes = law.update(box(.205, .53, .55), 1.2001)
    assert abs(axes["forward"] - previous["_axes"]["forward"]) <= .35 * scale * .0001 + 1e-12
    assert law.reference_height == previous["reference_height"]


@pytest.mark.parametrize("value", [None, True, 0, [], {}, "", "fast", "Normal"])
def test_bad_response_does_not_corrupt_active_law(value):
    law = law_for("gentle")
    law.adjust("closer")
    law.update(box(.205), 1.2)
    before = deepcopy(law.__dict__)
    for mutate in (lambda: law.set_range_response(value),
                   lambda: law.start(box(), 2., range_response=value)):
        with pytest.raises(ValueError, match="gentle, normal or responsive"):
            mutate()
        assert law.__dict__ == before


def engaged_helper(response="gentle"):
    helper = FramingControl(enabled=True)
    helper.set_range_response(response, 10.)
    helper.observe(observation())
    helper.select(7, CONTEXT, 10.)
    helper.engage(10.)
    helper.adjust("closer", 10.)
    helper.observe(observation(10.2, 2))
    helper.tick(10.2)
    return helper


def test_response_change_during_pause_cannot_extend_recovery_or_acknowledge_takeover():
    helper = engaged_helper()
    reference = helper.state(10.2)["reference_height"]
    helper.observe(observation(10.3, 3, detections=[]))
    with pytest.raises(RuntimeError, match="paused"):
        helper.set_range_response("responsive", 10.3)
    pause_deadline = 10.3 + DETECTION_PAUSE
    helper.observe(observation(10.6, 4, detections=[]))
    with pytest.raises(RuntimeError, match="paused"):
        helper.set_range_response("responsive", 10.6)
    assert helper.state(10.6)["range_response"] == "gentle"
    assert helper.state(10.6)["reference_height"] == reference
    # A fresh good image at the exact pause deadline does not rescue tracking.
    helper.observe(observation(pause_deadline, 5))
    with pytest.raises(RuntimeError, match="Take manual control"):
        helper.set_range_response("responsive", pause_deadline)
    loss = helper.last_loss
    assert loss["range_response"] == "gentle"
    assert loss["sequence"] == 3
    with pytest.raises(RuntimeError, match="Take manual control"):
        helper.set_range_response("normal", pause_deadline + 1.)
    assert helper.last_loss == loss
    assert helper.state(pause_deadline + 1.)["takeover_remaining_s"] == pytest.approx(1.)
    assert not helper.takeover_due(pause_deadline + 2. - .001)
    assert helper.takeover_due(pause_deadline + 2.)


def test_pause_resume_preserves_choice_and_reference_and_restarts_from_zero():
    helper = engaged_helper("responsive")
    reference = helper.state(10.2)["reference_height"]
    helper.observe(observation(10.3, 3, detections=[]))
    helper.tick(10.3)
    assert not any(helper.axes(10.3).values())
    helper.observe(observation(10.5, 4))
    helper.tick(10.5)
    state = helper.state(10.5)
    assert not state["paused"] and state["active"]
    assert state["range_response"] == "responsive" and state["reference_height"] == reference
    assert state["axes"]["forward"] == pytest.approx(.35 * math.log(1.1) * 1.35)


def response_request(f, value, **fields):
    return f["framing"]("response", range_response=value,
                        mode_generation=f["control"].state(f["now"][0])["mode_generation"], **fields)


@pytest.mark.parametrize("pilot_flight", [0, 2], indirect=True)
def test_http_response_choice_is_passive_and_preserves_pilot_profile_and_throttle(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    mode = c.state(f["now"][0])["selected_mode"]
    assert f["select"]().status_code == 200
    assert f["engage"](profile="pilot_throttle" if mode == 0 else "full").status_code == 200
    f["refresh"](.2)
    c.tick(f["link"], .2)
    before = c.state(.2)
    sent = len(f["link"].sent)
    result = response_request(f, "responsive")
    assert result.status_code == 200
    after = result.json()["control"]
    assert len(f["link"].sent) == sent
    assert after["throttle"] == before["throttle"]
    assert after["last_input_age"] == before["last_input_age"]
    for key in ("axes", "profile", "target_id", "reference_height", "height", "phase"):
        assert after["framing"][key] == before["framing"][key]
    assert after["framing"]["range_response"] == "responsive"
    assert after["framing"]["revision"] == before["framing"]["revision"] + 1


@pytest.mark.parametrize("value", [None, True, 0, [], {}, "", "fast", "Normal"])
def test_http_rejects_invalid_choice_without_changing_preference_or_selection(pilot_flight, value):
    f, c = pilot_flight, pilot_flight["control"]
    assert f["select"]().status_code == 200
    before = c.framing.state(f["now"][0])
    assert response_request(f, value).status_code == 422
    assert c.framing.state(f["now"][0]) == before


def test_http_choice_revision_prevents_engagement_using_old_view(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    assert f["select"]().status_code == 200
    revision = c.framing.revision
    assert response_request(f, "gentle").status_code == 200
    result = f["framing"]("engage", revision=revision, input_seq=f["seq"][0],
                           profile="pilot_throttle", mode_generation=0)
    assert result.status_code == 409 and "selection changed" in result.json()["detail"]
    assert c.framing.phase == "selected"
    assert f["engage"](profile="pilot_throttle").status_code == 200
    assert c.framing.state(f["now"][0])["range_response"] == "gentle"


def test_http_old_intent_and_foreign_owner_cannot_overwrite_new_choice(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    assert response_request(f, "gentle").status_code == 200
    before = c.framing.state(f["now"][0])
    request = {"token": f["token"], "operation": "response", "intent": f["intent"][0],
               "mode_generation": 0, "range_response": "responsive"}
    assert f["post"]("framing", request).status_code == 409
    request.update(token="another-browser", intent=999)
    assert f["post"]("framing", request).status_code == 409
    assert c.framing.state(f["now"][0]) == before
    assert c._framing_intent == f["intent"][0]


def test_http_choice_survives_stop_clear_and_new_owner_resets_normal(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    assert response_request(f, "gentle").status_code == 200
    assert f["select"]().status_code == 200
    assert f["engage"](profile="pilot_throttle").status_code == 200
    for operation in ("stop", "clear"):
        assert f["framing"](operation).status_code == 200
        assert c.framing.state(f["now"][0])["range_response"] == "gentle"
    assert f["post"]("action", {"token": f["token"], "action": "release"}).status_code == 200
    f["now"][0] = .2
    heartbeat(c, .2, armed=False, mode=9)
    simstate(c, .2)
    landed(c, .2, 1)
    claimed = f["post"]("claim", {})
    assert claimed.status_code == 200
    assert claimed.json()["token"] != f["token"]
    assert claimed.json()["control"]["framing"]["range_response"] == "normal"


@pytest.mark.parametrize("stale_generation", [None, 0, 1])
def test_http_mode_handoff_keeps_choice_and_rejects_pending_or_stale_changes(pilot_flight, stale_generation):
    f, c = pilot_flight, pilot_flight["control"]
    assert response_request(f, "gentle").status_code == 200
    hover(c, .05)
    thrust(c, .05)
    result = f["post"]("action", {"token": f["token"], "action": "switch_mode", "mode": 2,
                                    "mode_generation": 0, "input_seq": f["seq"][0]})
    assert result.status_code == 200
    pending = response_request(f, "responsive")
    assert pending.status_code == 409 and "flight mode change" in pending.json()["detail"]
    f["now"][0] = .2
    heartbeat(c, .2, armed=True, mode=2)
    assert f["pilot_input"](0.).status_code == 200
    assert c.state(.2)["mode_generation"] == 2
    assert c.framing.state(.2)["range_response"] == "gentle"
    accepted_intent = c._framing_intent
    request = {"token": f["token"], "operation": "response", "intent": 999,
               "range_response": "responsive"}
    if stale_generation is not None:
        request["mode_generation"] = stale_generation
    stale = f["post"]("framing", request)
    assert stale.status_code == 409
    assert c._framing_intent == accepted_intent
    assert c.framing.state(.2)["range_response"] == "gentle"
    assert response_request(f, "responsive").status_code == 200


def test_http_lost_target_refuses_choice_and_records_original_response(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    assert response_request(f, "gentle").status_code == 200
    assert f["select"]().status_code == 200
    assert f["engage"](profile="pilot_throttle").status_code == 200
    f["refresh"](.2, [{**PERSON, "track_id": 2}])
    result = response_request(f, "responsive")
    assert result.status_code == 409
    loss = c.framing.last_loss
    assert loss["range_response"] == "gentle"
    assert loss["profile"] == "pilot_throttle"
    assert c.framing.state(.2)["takeover_remaining_s"] == pytest.approx(2.)
    f["refresh"](.4)
    assert f["pilot_input"](.6).status_code == 200
    assert response_request(f, "normal").status_code == 409
    assert c.framing.last_loss == loss
    assert c.framing.state(.4)["takeover_remaining_s"] == pytest.approx(1.8)
    assert c.state(.4)["throttle"] == .6
