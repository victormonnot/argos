"""Airborne mode handoffs: wire order, measured gas, stale intent and fallback."""
from dataclasses import replace

import pytest

pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
from argos.backends.mavlink.link import SendStatus
from argos.console.control import (
    DO_SET_MODE, FlightControl, INPUT_TIMEOUT, MODE_CHANGE_TIMEOUT, ModeGenerationConflict,
)
from test_console_flight_control import (
    ZERO, ack, armed, event, heartbeat, landed, mav, profile, simstate,
)


def thrust(control, now=.1, value=.35, **changes):
    message = event(mav.MAVLink_attitude_target_message(
        100, 0, [1., 0., 0., 0.], 0., 0., 0., value), now)
    control.append(replace(message, fields={**message.fields, **changes}), now=now)


def hover(control, now=.1, value=.35, **changes):
    message = event(mav.MAVLink_param_value_message(b"MOT_THST_HOVER", value, 9, 1, 0), now)
    control.append(replace(message, fields={**message.fields, **changes}), now=now)


def flying(mode=2):
    control, link, token = armed(mode)
    landed(control, .1, 2)
    control.input(token, 0, ZERO, throttle=.58 if mode == 0 else 0, link=link, now=.1)
    thrust(control)
    hover(control)
    return control, link, token


def switch(control, link, token, now=.2, **changes):
    state = control.state(now)
    return control.action(token, "switch_mode", link=link, now=now, **{
        "mode": 0 if state["selected_mode"] == 2 else 2,
        "mode_generation": state["mode_generation"], "input_seq": state["input_seq"],
        **changes,
    })


@pytest.mark.parametrize("mode", [0, 2])
def test_mode_switch_waits_for_target_heartbeat_and_keeps_only_bounded_bridge(mode):
    control, link, token = flying(mode)
    before = len(link.sent)
    state = switch(control, link, token)
    target = 2 if mode == 0 else 0
    bridge = state["mode_transition"]["bridge_throttle"]
    messages = [message for message, _ in link.sent[before:]]
    assert [message.get_type() for message in messages] == ["MANUAL_CONTROL", "COMMAND_LONG"]
    assert (messages[0].x, messages[0].y, messages[0].z, messages[0].r) == (0, 0, round(bridge * 1000), 0)
    assert messages[1].command == DO_SET_MODE and messages[1].param2 == target
    assert .25 <= bridge <= .75
    assert state["phase"] == "switching" and state["mode_generation"] == 1
    assert state["selected_mode"] == mode and state["mode_transfer"] is None
    ack(control, DO_SET_MODE, 0, .21)
    assert control.state(.21)["command"]["state"] == "accepted"
    heartbeat(control, .22, armed=True, mode=mode)
    count = len(link.messages("MANUAL_CONTROL"))
    control.input(token, 1, ZERO, throttle=.58 if mode == 0 else 0,
                  mode_generation=1, link=link, now=.3)
    control.tick(link, .3)
    assert len(link.messages("MANUAL_CONTROL")) == count
    assert control.state(.3)["last_input_age"] == 0
    heartbeat(control, .4, armed=True, mode=target)
    observed = control.state(.4)
    assert observed["phase"] == "armed" and observed["mode_transfer"]["generation"] == 2
    control.tick(link, .4)
    state = control.state(.4)
    assert state["selected_mode"] == target and state["prepared"]
    assert state["mode_generation"] == 2 and state["mode_transition"] is None
    assert state["command"]["observed"] and state["phase"] == "armed"
    assert state["mode_transfer"] == {
        "generation": 2, "from_mode": mode, "to_mode": target,
        "completed_at": .4, "throttle": bridge if target == 0 else 0,
    }
    message = link.messages("MANUAL_CONTROL")[-1]
    assert message.z == (round(bridge * 1000) if target == 0 else 500)
    assert (message.x, message.y, message.r) == (0, 0, 0)


@pytest.mark.parametrize("generation", [None, 0, 2])
def test_stale_mode_input_never_updates_sequence_throttle_or_lease(generation):
    control, link, token = flying(0)
    switch(control, link, token)
    extra = {} if generation is None else {"mode_generation": generation}
    with pytest.raises(ModeGenerationConflict) as error:
        control.input(token, 999, ZERO, throttle=.7, link=link, now=.3, **extra)
    state = error.value.control
    assert state["mode_generation"] == 1 and state["owned"]
    assert state["input_seq"] == 0 and state["throttle"] == .58
    assert state["last_input_age"] == pytest.approx(.2)


def test_pending_generation_input_cannot_leak_after_confirmation():
    control, link, token = flying(0)
    switch(control, link, token)
    heartbeat(control, .3, armed=True, mode=2)
    with pytest.raises(ModeGenerationConflict):
        control.input(token, 99, ZERO, throttle=.58, mode_generation=1, link=link, now=.4)
    control.input(token, 1, ZERO, mode_generation=2, link=link, now=.4)
    assert control.state(.4)["input_seq"] == 1


def test_unchanged_stabilize_keepalive_does_not_supersede_the_mode_button():
    control, link, token = flying(0)
    control.input(token, 1, ZERO, throttle=.58, link=link, now=.15)
    assert switch(control, link, token, input_seq=0)["phase"] == "switching"


@pytest.mark.parametrize("changed", [{"throttle": .6}, {"axes": {**ZERO, "yaw": .1}}])
def test_new_pilot_change_supersedes_old_mode_button_even_after_release(changed):
    control, link, token = flying(0)
    values = {"throttle": .58, "axes": ZERO, **changed}
    control.input(token, 1, values.pop("axes"), link=link, now=.15, **values)
    control.input(token, 2, ZERO, throttle=.58, link=link, now=.16)
    with pytest.raises(RuntimeError, match="superseded"):
        switch(control, link, token, input_seq=0)
    assert control.state(.2)["mode_generation"] == 0


@pytest.mark.parametrize("fault", ["denied", "timeout", "lease", "other_mode", "disarm", "link", "profile"])
def test_uncertain_transition_revokes_once_with_land_without_reinterpreted_manual(fault):
    control, link, token = flying()
    switch(control, link, token)
    before = len(link.messages("MANUAL_CONTROL"))
    now = .3
    if fault == "denied":
        ack(control, DO_SET_MODE, 2, now)
    elif fault == "timeout":
        for seq, at in enumerate((.4, .7, 1.), 1):
            control.input(token, seq, ZERO, mode_generation=1, link=link, now=at)
            heartbeat(control, at, armed=True, mode=2)
        now = .2 + MODE_CHANGE_TIMEOUT
    elif fault == "lease":
        now = .1 + INPUT_TIMEOUT
    elif fault == "other_mode":
        heartbeat(control, now, armed=True, mode=5)
    elif fault == "disarm":
        heartbeat(control, now, armed=False, mode=2)
    elif fault == "profile":
        profile(control, now, RC_OPTIONS=2)
    control.tick(None if fault == "link" else link, now)
    state = control.state(now)
    assert not state["owned"] and state["mode_transition"] is None
    assert len(link.messages("MANUAL_CONTROL")) == before
    assert state["interruption"] is not None
    if fault not in ("link", "disarm"):
        assert state["command"]["action"] == "land"
        assert link.messages("COMMAND_LONG")[-1].param2 == 9
    count = len(link.sent)
    control.tick(link, now + .1)
    assert len(link.sent) == count


@pytest.mark.parametrize("fault", ["deadline", "expired_lease", "profile", "landed"])
def test_target_heartbeat_cannot_complete_an_already_invalid_transition(fault):
    control, link, token = flying(0)
    switch(control, link, token)
    now = .3
    if fault == "deadline":
        for seq, at in enumerate((.4, .7, 1.), 1):
            control.input(token, seq, ZERO, throttle=.58, mode_generation=1, link=link, now=at)
        now = .2 + MODE_CHANGE_TIMEOUT
    elif fault == "expired_lease":
        now = .1 + INPUT_TIMEOUT
    elif fault == "profile":
        profile(control, now, RC3_REVERSED=1)
    else:
        landed(control, now, 1)
    heartbeat(control, now, armed=True, mode=2)
    assert control.state(now)["mode_generation"] == 1
    assert control.state(now)["mode_transfer"] is None
    control.tick(link, now)
    assert not control.state(now)["owned"]


@pytest.mark.parametrize("send_number", [1, 2])
def test_failed_bridge_or_command_sends_land_and_does_not_resume_manual(send_number):
    control, link, token = flying()
    original = link.send
    count = [0]
    def fail_one(message, now):
        count[0] += 1
        link.status = SendStatus.PARTIAL if count[0] == send_number else SendStatus.ACCEPTED
        return original(message, now)
    link.send = fail_one
    state = switch(control, link, token)
    assert not state["owned"] and state["command"]["action"] == "land"
    assert state["mode_transition"] is None
    assert link.messages("COMMAND_LONG")[-1].param2 == 9


@pytest.mark.parametrize("fault", ["thrust_missing", "thrust_stale", "hover_missing", "hover_stale", "too_low", "too_high"])
def test_unsafe_or_unmeasured_transfer_is_refused_without_changing_flight(fault):
    control, link, token = flying()
    if fault == "thrust_missing":
        control._attitude_thrust = None
    elif fault == "thrust_stale":
        control._attitude_thrust = (.35, -.3)
    elif fault == "hover_missing":
        control._hover_thrust = None
    elif fault == "hover_stale":
        control._hover_thrust = (.35, -1)
    else:
        thrust(control, .15, .001 if fault == "too_low" else .999)
    count = len(link.sent)
    with pytest.raises(RuntimeError):
        switch(control, link, token)
    state = control.state(.2)
    assert state["owned"] and state["selected_mode"] == 2 and state["mode_generation"] == 0
    assert not state["mode_switch"]["available"] and len(link.sent) == count


@pytest.mark.parametrize("changes", [{"thrust": float("nan")}, {"thrust": -1}, {"thrust": 2},
                                     {"type_mask": 64}, {"type_mask": 32}, {"type_mask": True},
                                     {"time_boot_ms": -1}])
def test_invalid_current_thrust_receipt_invalidates_readiness(changes):
    control, link, token = flying()
    thrust(control, .15, **changes)
    assert not control.state(.2)["mode_switch"]["available"]


@pytest.mark.parametrize("changes", [{"param_value": float("nan")}, {"param_value": .1},
                                     {"param_value": .7}, {"param_type": 6}])
def test_invalid_current_hover_receipt_invalidates_readiness(changes):
    control, link, token = flying()
    hover(control, .15, **changes)
    assert not control.state(.2)["mode_switch"]["available"]


@pytest.mark.parametrize("kind", ["thrust", "hover"])
def test_late_valid_receipt_cannot_resurrect_newer_invalid_throttle_evidence(kind):
    control, link, token = flying()
    if kind == "thrust":
        thrust(control, .16, value=float("nan"))
        thrust(control, .15)
    else:
        hover(control, .16, value=float("nan"))
        hover(control, .15)
    assert not control.state(.2)["mode_switch"]["available"]


def test_framing_is_cleared_and_old_engage_stays_invalid_after_round_trip():
    from argos.console.framing import FramingControl
    from test_console_framing import observation
    control, link, token = flying()
    control.framing = FramingControl(enabled=True)
    control.framing.observe(observation(.1, 1))
    control.framing.select(7, ("run-one", "video-one"), .1)
    control.framing.engage(.1)
    revision = control.framing.revision
    switch(control, link, token)
    assert control.framing.phase == "idle" and control.framing.revision > revision
    heartbeat(control, .3, armed=True, mode=0)
    gas = control.state(.3)["throttle"]
    control.input(token, 1, ZERO, throttle=gas, mode_generation=2, link=link, now=.3)
    switch(control, link, token, .4)
    heartbeat(control, .5, armed=True, mode=2)
    control.input(token, 2, ZERO, mode_generation=4, link=link, now=.5)
    with pytest.raises(RuntimeError, match="selection changed"):
        control.framing_request({"token": token, "operation": "engage", "intent": 1,
            "revision": revision, "input_seq": 0, "mode_generation": 4}, link=link, now=.5,
            selection_check=lambda *_: None)
    assert control.framing.phase == "idle"


@pytest.mark.parametrize("operation", ["select", "engage", "closer", "farther"])
@pytest.mark.parametrize("generation", [None, 0, 1, 2, 3])
def test_delayed_framing_intent_cannot_restore_a_target_after_mode_round_trip(operation, generation):
    from argos.console.framing import FramingControl
    from test_console_framing import observation
    control, link, token = flying()
    control.framing = FramingControl(enabled=True)
    control.framing.observe(observation(.1, 1))
    control.framing.select(7, ("run-one", "video-one"), .1)
    revision = control.framing.revision
    switch(control, link, token)
    heartbeat(control, .3, armed=True, mode=0)
    control.input(token, 1, ZERO, throttle=control.state(.3)["throttle"],
                  mode_generation=2, link=link, now=.3)
    switch(control, link, token, .4)
    heartbeat(control, .5, armed=True, mode=2)
    control.input(token, 2, ZERO, mode_generation=4, link=link, now=.5)
    extra = ({"run_id": "run-one", "video_id": "video-one", "frame_sequence": 1, "track_id": 7}
             if operation == "select" else {"revision": revision, "input_seq": 0}
             if operation == "engage" else {})
    request = {"token": token, "operation": operation, "intent": 99, **extra}
    if generation is not None:
        request["mode_generation"] = generation
    checked = []
    with pytest.raises(ModeGenerationConflict):
        control.framing_request(request, link=link, now=.5,
                                selection_check=lambda *_: checked.append(True))
    assert control.framing.phase == "idle" and control.framing.state(.5)["target_id"] is None
    assert control._framing_intent == 0 and checked == []
    # A new explicit selection from the current mode remains valid, including
    # with a lower intent than the refused delayed browser request.
    control.framing_request({"token": token, "operation": "select", "intent": 1,
        "mode_generation": 4, "run_id": "run-one", "video_id": "video-one",
        "frame_sequence": 2, "track_id": 7}, link=link, now=.5,
        selection_check=lambda *_: checked.append(True))
    assert checked == [True] and control.framing.state(.5)["target_id"] == 7


def test_current_generation_selection_waits_for_mode_confirmation_but_stop_and_clear_work():
    from argos.console.framing import FramingControl
    control, link, token = flying()
    control.framing = FramingControl(enabled=True)
    switch(control, link, token)
    checked = []
    with pytest.raises(RuntimeError, match="flight mode change"):
        control.framing_request({"token": token, "operation": "select", "intent": 1,
            "mode_generation": 1, "run_id": "run-one", "video_id": "video-one",
            "frame_sequence": 1, "track_id": 7}, link=link, now=.3,
            selection_check=lambda *_: checked.append(True))
    assert checked == [] and control.framing.state(.3)["target_id"] is None
    for intent, operation in ((2, "stop"), (3, "clear")):
        control.framing_request({"token": token, "operation": operation, "intent": intent},
            link=link, now=.3, selection_check=lambda *_: None)
    assert control.framing.phase == "idle"


def test_hover_reads_are_bounded_and_stop_with_landing():
    control, link, token = flying()
    control.tick(link, .2)
    before = len([m for m in link.messages("PARAM_REQUEST_READ") if m.param_id == "MOT_THST_HOVER"])
    for seq, now in enumerate((.3, .4, .5, .6), 1):
        control.input(token, seq, ZERO, link=link, now=now)
        control.tick(link, now)
    assert len([m for m in link.messages("PARAM_REQUEST_READ") if m.param_id == "MOT_THST_HOVER"]) == before
    control.input(token, 5, ZERO, link=link, now=.71)
    control.tick(link, .71)
    assert len([m for m in link.messages("PARAM_REQUEST_READ") if m.param_id == "MOT_THST_HOVER"]) == before + 1
    control.action(token, "land", link=link, now=.8)
    count = len(link.sent)
    control.input(token, 6, ZERO, link=link, now=1.)
    control.tick(link, 1.)
    assert len(link.sent) == count


def test_pending_mode_pauses_gcs_so_failsafe_keeps_margin_over_bridge_override():
    control, link, token = flying()
    switch(control, link, token)
    count = len(link.sent)
    # The normal GCS interval is due at 1.0, before the 1.2 mode deadline.
    for seq, now in enumerate((.4, .7, 1.0), 1):
        control.input(token, seq, ZERO, mode_generation=1, link=link, now=now)
        heartbeat(control, now, armed=True, mode=2)
        control.tick(link, now)
    assert len(link.sent) == count
    link.status = SendStatus.BLOCKED
    control.tick(link, 1.2)
    assert not control.state(1.2)["owned"]
    assert control.state(1.2)["command"]["action"] == "land"
    assert [at for message, at in link.sent if message.get_type() == "HEARTBEAT"] == [0.]
    assert max(at for message, at in link.sent if message.get_type() == "MANUAL_CONTROL") == .2


@pytest.mark.parametrize("hover_value", [.125, .2, .35, .5, .6875])
def test_transfer_inverse_reconstructs_demand_through_actual_digital_steps(hover_value):
    expo = max(-.5, min(1., (.5 - hover_value) / .375))
    for demand in (.05, .15, .3, .4, .6, .8):
        throttle = FlightControl._inverse_stabilize_throttle(demand, hover_value)
        pulse = 1100 + int(800 * round(throttle * 1000) / 1000)
        control_in = int(1000 * (pulse - 1100) / 800)
        t = control_in / 1000
        actual = t * (1 - expo) + expo * t**3
        assert actual == pytest.approx(demand, abs=.004)
    assert FlightControl._inverse_stabilize_throttle(.35, .35) != pytest.approx(.35, abs=.01)


def test_new_claim_resets_transfer_generation_and_dynamic_throttle_evidence():
    control, link, token = flying()
    switch(control, link, token)
    heartbeat(control, .3, armed=True, mode=0)
    control.action(token, "land", link=link, now=.4)
    heartbeat(control, .5, mode=9)
    landed(control, .5, 1)
    control.action(token, "release", link=link, now=.5)
    state = control.claim(link, .5)["control"]
    assert state["mode_generation"] == 0 and state["mode_transfer"] is None
    assert control._attitude_thrust is None and control._hover_thrust is None
