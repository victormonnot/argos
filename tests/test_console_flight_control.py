"""Manual SITL control: leases, flight evidence, failure paths and wire intent."""
from dataclasses import replace

import pytest

mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")

from argos.backends.mavlink.link import Received, SendResult, SendStatus
from argos.console.control import (
    ARM_DISARM, DO_SET_MODE, FlightControl, INPUT_TIMEOUT,
    REQUIRED_PARAMETERS,
)


ZERO = {"forward": 0., "right": 0., "up": 0., "yaw": 0.}


class Link:
    def __init__(self):
        self.sent = []
        self.status = SendStatus.ACCEPTED

    def send(self, message, now):
        self.sent.append((message, now))
        return SendResult(self.status, 10 if self.status == SendStatus.ACCEPTED else 0)

    def messages(self, name):
        return [message for message, _ in self.sent if message.get_type() == name]


def event(message, now=0., *, system=1, component=1):
    return Received(now, system, component, 0, message.get_msgId(), message.get_type(),
                    message.to_dict(), b"")


def heartbeat(control, now=0., *, armed=False, mode=2, system=1, component=1,
              autopilot=3, kind=2):
    control.append(event(mav.MAVLink_heartbeat_message(
        kind, autopilot, 1 | (128 if armed else 0), mode, 4, 3), now,
        system=system, component=component), now=now)


def simstate(control, now=0., **source):
    control.append(event(mav.MAVLink_simstate_message(*([0.] * 9), 0, 0), now,
                         **source), now=now)


def profile(control, now=0., **overrides):
    for key, required in REQUIRED_PARAMETERS.items():
        control.append(event(mav.MAVLink_param_value_message(
            key.encode(), overrides.get(key, required), 9, len(REQUIRED_PARAMETERS), 0),
            now), now=now)


def ack(control, command, result, now=0., **source):
    control.append(event(mav.MAVLink_command_ack_message(command, result), now, **source), now=now)


def landed(control, now=0., state=1):
    control.append(event(mav.MAVLink_extended_sys_state_message(0, state), now), now=now)


def claimed():
    control, link = FlightControl(enabled=True), Link()
    heartbeat(control)
    simstate(control)
    response = control.claim(link, 0.)
    profile(control)
    return control, link, response["token"]


def armed():
    control, link, token = claimed()
    control.action(token, "arm", link=link, now=.01)
    ack(control, ARM_DISARM, 0, .02)
    heartbeat(control, .03, armed=True)
    return control, link, token


def test_disabled_and_unclaimed_services_are_strictly_passive():
    for enabled in (False, True):
        control, link = FlightControl(enabled=enabled), Link()
        heartbeat(control)
        simstate(control)
        control.tick(link, 0.)
        control.tick(link, 10.)
        control.close(link, 11.)
        assert link.sent == []
        assert control.state(11.)["owned"] is False


@pytest.mark.parametrize("bad_source", [{"system": 2}, {"component": 42}])
def test_claim_requires_selected_source_for_both_heartbeat_and_simstate(bad_source):
    control, link = FlightControl(enabled=True), Link()
    heartbeat(control, **bad_source)
    simstate(control)
    with pytest.raises(RuntimeError, match="HEARTBEAT"):
        control.claim(link, 0.)
    heartbeat(control)
    control._simstate_at = None
    simstate(control, **bad_source)
    with pytest.raises(RuntimeError, match="SIMSTATE"):
        control.claim(link, 0.)
    assert link.sent == []


@pytest.mark.parametrize("changes", [{"autopilot": 12}, {"kind": 1}, {"armed": True}])
def test_claim_refuses_other_autopilots_or_already_armed_vehicle(changes):
    control, link = FlightControl(enabled=True), Link()
    heartbeat(control, **changes)
    simstate(control)
    with pytest.raises(RuntimeError):
        control.claim(link, 0.)
    assert link.sent == []


def test_claim_requires_fresh_receipts_and_does_not_use_simulator_coordinates():
    control, link = FlightControl(enabled=True), Link()
    heartbeat(control)
    simstate(control)
    with pytest.raises(RuntimeError, match="HEARTBEAT"):
        control.claim(link, 2.01)
    heartbeat(control, 3.01)
    with pytest.raises(RuntimeError, match="SIMSTATE"):
        control.claim(link, 3.01)
    assert not any(key in control.__dict__ for key in ("lat", "lng", "position"))


def test_claim_only_requests_parameters_and_stream_then_low_throttle_and_gcs_heartbeat():
    control, link, token = claimed()
    requests = link.messages("PARAM_REQUEST_READ")
    assert {msg.param_id for msg in requests} == set(REQUIRED_PARAMETERS)
    assert all(msg.param_index == -1 for msg in requests)
    commands = link.messages("COMMAND_LONG")
    assert len(commands) == 2
    assert all(command.command == 511 and command.param2 == 200000 for command in commands)
    assert {command.param1 for command in commands} == {0, 245}
    manual, = link.messages("MANUAL_CONTROL")
    assert (manual.x, manual.y, manual.z, manual.r) == (0, 0, 0, 0)
    assert link.messages("HEARTBEAT")[0].type == mav.MAV_TYPE_GCS
    assert token and "token" not in control.state(0.)
    assert control.state(0.)["command"] is None
    assert not link.messages("PARAM_SET")


def test_claim_is_exclusive_and_failed_param_request_does_not_create_owner():
    control, link, token = claimed()
    with pytest.raises(RuntimeError, match="détenu"):
        control.claim(link, .1)
    with pytest.raises(RuntimeError):
        control.input("other", 0, ZERO, link=link, now=.1)
    control.action(token, "release", link=link, now=.1)
    link.status = SendStatus.BLOCKED
    with pytest.raises(RuntimeError, match="paramètres"):
        control.claim(link, .2)
    assert control.state(.2)["owned"] is False


@pytest.mark.parametrize("parameters", [{"GPS1_TYPE": 1}, {"GPS2_TYPE": 1},
                                      {"FS_GCS_ENABLE": 0}, {"FS_GCS_TIMEOUT": 5},
                                      {"RC_OVERRIDE_TIME": -1}, {"FS_OPTIONS": 16},
                                      {"FLTMODE_CH": 5}, {"MAV_GCS_SYSID": 1},
                                      {"PILOT_SPD_UP": 2.5}, {"PILOT_SPD_DN": 1.5},
                                      {"LAND_SPD_MS": 1.}, {"ATC_ANGLE_MAX": 45.}])
def test_arming_refuses_mismatched_profile_without_modifying_parameters(parameters):
    control, link, token = claimed()
    profile(control, .01, **parameters)
    with pytest.raises(RuntimeError, match="Profil"):
        control.action(token, "arm", link=link, now=.02)
    assert all(msg.command != ARM_DISARM for msg in link.messages("COMMAND_LONG"))
    assert not link.messages("PARAM_SET")


def test_arming_requires_new_parameter_receipts_for_this_claim_and_observed_alt_hold():
    control, link = FlightControl(enabled=True), Link()
    heartbeat(control, mode=0)
    simstate(control)
    profile(control)  # unsolicited parameters before claim do not fulfill its request
    token = control.claim(link, 0.)["token"]
    with pytest.raises(RuntimeError, match="Profil"):
        control.action(token, "arm", link=link, now=.01)
    profile(control, .02)
    with pytest.raises(RuntimeError, match="AltHold"):
        control.action(token, "arm", link=link, now=.03)
    control.action(token, "prepare", link=link, now=.04)
    mode_command = link.messages("COMMAND_LONG")[-1]
    assert (mode_command.command, mode_command.param1, mode_command.param2) == (176, 1, 2)
    ack(control, DO_SET_MODE, 0, .05)
    with pytest.raises(RuntimeError, match="confirmation"):
        control.action(token, "arm", link=link, now=.06)
    heartbeat(control, .07, mode=2)
    control.action(token, "arm", link=link, now=.08)
    arm_command = link.messages("COMMAND_LONG")[-1]
    assert (arm_command.command, arm_command.param1, arm_command.param2) == (400, 1, 0)


@pytest.mark.parametrize("action", ["prepare", "arm"])
def test_prepare_and_arm_require_released_controls(action):
    control, link, token = claimed()
    control.input(token, 0, {**ZERO, "forward": .2}, link=link, now=.1)
    with pytest.raises(RuntimeError, match="Relâchez"):
        control.action(token, action, link=link, now=.1)


def test_send_ack_and_observed_state_are_distinct_without_duplicate_arm():
    control, link, token = claimed()
    sent = control.action(token, "arm", link=link, now=.01)["command"]
    assert sent["transport"] == "accepted"
    assert sent["state"] == "sent" and sent["ack"] is None and not sent["observed"]
    with pytest.raises(RuntimeError, match="confirmation"):
        control.action(token, "arm", link=link, now=.02)
    ack(control, ARM_DISARM, 0, .03, component=42)
    assert control.state(.03)["command"]["ack"] is None
    ack(control, ARM_DISARM, 0, .04)
    accepted = control.state(.04)["command"]
    assert accepted["state"] == "accepted" and not accepted["observed"]
    heartbeat(control, .05, armed=True)
    observed = control.state(.05)["command"]
    assert observed["state"] == "observed" and observed["observed"]
    assert observed["ack"] == 0 and observed["observed_at"] == .05
    with pytest.raises(RuntimeError, match="Désarmement"):
        control.action(token, "arm", link=link, now=.06)
    assert sum(msg.command == ARM_DISARM for msg in link.messages("COMMAND_LONG")) == 1


def test_denied_arming_and_missing_confirmation_are_explicit_and_never_retried():
    control, link, token = claimed()
    control.action(token, "arm", link=link, now=.01)
    ack(control, ARM_DISARM, 2, .02)
    assert control.state(.02)["command"]["state"] == "denied"
    assert "refusée" in control.state(.02)["last_error"]
    control.action(token, "arm", link=link, now=.03)  # new explicit operator attempt
    for i in range(1, 46):
        now = i * .1
        heartbeat(control, now)
        simstate(control, now)
        control.input(token, i, ZERO, link=link, now=now)
        control.tick(link, now)
    assert control.state(4.5)["command"]["state"] == "timeout"
    assert sum(msg.command == ARM_DISARM for msg in link.messages("COMMAND_LONG")) == 2
    with pytest.raises(RuntimeError, match="Désarmement"):
        control.action(token, "arm", link=link, now=4.51)


def test_armed_manual_axes_have_correct_sign_bounds_and_neutral_vertical_mapping():
    control, link, token = armed()
    control.input(token, 0, {"forward": 1., "right": -.5, "up": .4, "yaw": -.2},
                  link=link, now=.1)
    control.tick(link, .1)
    msg = link.messages("MANUAL_CONTROL")[-1]
    assert (msg.x, msg.y, msg.z, msg.r) == (300, -150, 700, -60)
    control.input(token, 1, ZERO, link=link, now=.2)
    control.tick(link, .2)
    msg = link.messages("MANUAL_CONTROL")[-1]
    assert (msg.x, msg.y, msg.z, msg.r) == (0, 0, 500, 0)


@pytest.mark.parametrize("axes", [{}, {**ZERO, "extra": 0}, {**ZERO, "up": True},
                                {**ZERO, "up": float("nan")}, {**ZERO, "up": float("inf")},
                                {**ZERO, "up": 1.01}, {**ZERO, "up": -1.01},
                                {**ZERO, "up": 10**1000}])
def test_invalid_input_does_not_refresh_deadman(axes):
    control, link, token = claimed()
    with pytest.raises(ValueError, match="axes"):
        control.input(token, 0, axes, link=link, now=.4)
    assert control.state(.4)["last_input_age"] == .4
    control.tick(link, INPUT_TIMEOUT)
    assert not control.state(INPUT_TIMEOUT)["owned"]


def test_out_of_order_request_cannot_restore_a_released_maneuver():
    control, link, token = armed()
    control.input(token, 10, {**ZERO, "forward": 1}, link=link, now=.1)
    control.input(token, 12, ZERO, link=link, now=.2)
    with pytest.raises(ValueError, match="séquence"):
        control.input(token, 11, {**ZERO, "forward": 1}, link=link, now=.3)
    control.tick(link, .3)
    assert link.messages("MANUAL_CONTROL")[-1].x == 0
    assert control.state(.3)["last_input_age"] == pytest.approx(.1)


def test_delayed_input_expires_before_refresh_and_cannot_reclaim_armed_vehicle():
    control, link, token = armed()
    control.input(token, 0, {**ZERO, "up": 1}, link=link, now=.1)
    with pytest.raises(RuntimeError, match="expiré"):
        control.input(token, 1, ZERO, link=link, now=.1 + INPUT_TIMEOUT)
    snapshot = control.state(.8)
    assert snapshot["phase"] == "expired" and not snapshot["owned"]
    land_command = link.messages("COMMAND_LONG")[-1]
    assert land_command.command == DO_SET_MODE and land_command.param2 == 9
    with pytest.raises(RuntimeError, match="désarmement"):
        control.claim(link, .8)
    before = len(link.sent)
    control.tick(link, .9)
    control.tick(link, 2.)
    assert len(link.sent) == before  # no heartbeat masking the GCS failsafe


def test_release_revokes_token_and_neutralizes_before_one_land_without_forced_disarm():
    control, link, token = armed()
    control.input(token, 0, {**ZERO, "forward": 1}, link=link, now=.1)
    control.tick(link, .1)
    result = control.action(token, "release", link=link, now=.2)
    assert not result["owned"]
    last_manual = link.messages("MANUAL_CONTROL")[-1]
    assert (last_manual.x, last_manual.y, last_manual.z, last_manual.r) == (0, 0, 500, 0)
    assert link.messages("COMMAND_LONG")[-1].param2 == 9
    with pytest.raises(RuntimeError):
        control.action(token, "arm", link=link, now=.3)
    assert not any(msg.command == ARM_DISARM and msg.param1 == 0
                   for msg in link.messages("COMMAND_LONG"))


def test_pending_arm_expiry_also_attempts_land_and_blocks_source_reconfigure():
    control, link, token = claimed()
    control.action(token, "arm", link=link, now=.1)
    control.tick(link, INPUT_TIMEOUT)
    assert link.messages("COMMAND_LONG")[-1].param2 == 9
    with pytest.raises(RuntimeError):
        control.check_reconfigure()


def test_delayed_arm_ack_and_buffered_disarmed_heartbeat_cannot_resolve_expired_arm():
    control, link, token = claimed()
    control.action(token, "arm", link=link, now=.1)
    control.tick(link, INPUT_TIMEOUT)
    ack(control, ARM_DISARM, 0, .7)  # earlier action; does not acknowledge LAND
    assert control.state(.7)["command"]["ack"] is None
    heartbeat(control, .71, armed=False, mode=2)
    with pytest.raises(RuntimeError):
        control.check_reconfigure()
    with pytest.raises(RuntimeError, match="désarmement"):
        control.claim(link, .72)
    ack(control, DO_SET_MODE, 0, .73)
    with pytest.raises(RuntimeError):
        control.check_reconfigure()  # ACK alone is insufficient
    heartbeat(control, .74, armed=True, mode=9)
    with pytest.raises(RuntimeError):
        control.check_reconfigure()
    heartbeat(control, .75, armed=False, mode=9)
    control.check_reconfigure()
    assert control.claim(link, .76)["token"] != token


def test_explicit_land_suppresses_later_maneuvers_even_with_valid_input():
    control, link, token = armed()
    result = control.action(token, "land", link=link, now=.1)
    assert result["phase"] == "landing" and result["owned"]
    manual_count = len(link.messages("MANUAL_CONTROL"))
    control.input(token, 0, {**ZERO, "up": 1, "forward": 1}, link=link, now=.2)
    heartbeat(control, .2, armed=True, mode=9)
    control.tick(link, .2)
    assert len(link.messages("MANUAL_CONTROL")) == manual_count
    assert control.state(.2)["command"]["observed"]
    with pytest.raises(RuntimeError, match="déjà"):
        control.action(token, "land", link=link, now=.3)


def test_disarm_requires_fresh_selected_landed_state_and_never_uses_force():
    control, link, token = armed()
    for state in (None, 0, 2):
        if state is not None:
            landed(control, .1, state)
        with pytest.raises(RuntimeError, match="posé"):
            control.action(token, "disarm", link=link, now=.1)
    landed(control, .2, 1)
    result = control.action(token, "disarm", link=link, now=.2)
    assert not result["command"]["observed"]
    msg = link.messages("COMMAND_LONG")[-1]
    assert (msg.command, msg.param1, msg.param2) == (400, 0, 0)
    heartbeat(control, .3, armed=False)
    assert control.state(.3)["command"]["observed"]


def test_stale_landed_state_never_allows_disarm():
    control, link, token = armed()
    landed(control, .1)
    for i in range(1, 26):
        now = i * .1
        heartbeat(control, now, armed=True)
        simstate(control, now)
        control.input(token, i, ZERO, link=link, now=now)
    assert control.state(2.5)["vehicle"]["landed"] is None
    with pytest.raises(RuntimeError, match="posé"):
        control.action(token, "disarm", link=link, now=2.5)


def test_link_loss_releases_and_never_sends_again_on_a_reappearing_transport():
    control, link, token = armed()
    before = len(link.sent)
    control.tick(None, .1)
    assert len(link.sent) == before
    assert not control.state(.1)["available"] and not control.state(.1)["owned"]
    control.tick(link, .2)
    assert len(link.sent) == before
    with pytest.raises(RuntimeError):
        control.check_reconfigure()


def test_source_reconfiguration_is_blocked_by_owner_or_observed_armed_state():
    control, link, token = claimed()
    with pytest.raises(RuntimeError):
        control.check_reconfigure()
    control.action(token, "release", link=link, now=.1)
    control.check_reconfigure()
    heartbeat(control, .2, armed=True)
    with pytest.raises(RuntimeError):
        control.check_reconfigure()
    heartbeat(control, .3)
    control.check_reconfigure()


def test_failure_to_send_manual_input_revokes_instead_of_queueing_old_motion():
    control, link, token = armed()
    control.input(token, 0, {**ZERO, "forward": 1}, link=link, now=.1)
    link.status = SendStatus.BLOCKED
    control.tick(link, .1)
    assert not control.state(.1)["owned"]
    assert control.state(.1)["command"]["state"] == "send_failed"
    before = len(link.sent)
    link.status = SendStatus.ACCEPTED
    control.tick(link, .2)
    assert len(link.sent) == before


def test_parameter_change_while_armed_revokes_and_requests_landing():
    control, link, token = armed()
    profile(control, .1, GPS1_TYPE=1)
    control.tick(link, .1)
    assert control.state(.1)["phase"] == "error"
    assert not control.state(.1)["owned"]
    assert link.messages("COMMAND_LONG")[-1].param2 == 9


def test_invalid_or_older_heartbeat_and_future_receipts_do_not_refresh_identity():
    control, link, _token = claimed()
    original = event(mav.MAVLink_heartbeat_message(2, 3, 129, 2, 4, 3), .5)
    control.append(replace(original, fields={**original.fields, "base_mode": True}), now=.5)
    control.append(replace(original, received_at=1.), now=.5)
    assert control.state(.5)["vehicle"]["heartbeat_age"] == .5
    assert control.state(.5)["vehicle"]["armed"] is False
    heartbeat(control, .4, armed=True)
    heartbeat(control, .3, armed=False)
    assert control.state(.5)["vehicle"]["armed"] is True


def test_state_is_pure_and_does_not_expose_or_refresh_lease():
    control, link, token = claimed()
    before = len(link.sent)
    first = control.state(.2)
    assert first["at"] == .2 and first["last_input_age"] == .2
    assert token not in str(first)
    first["axes"]["up"] = 1
    first["profile"]["values"]["GPS1_TYPE"] = 1
    assert control.state(.3)["axes"]["up"] == 0
    assert control.state(.3)["profile"]["ready"]
    assert len(link.sent) == before
