"""Lease interruption evidence survives the autopilot landing/disarm aftermath."""
import pytest

pytest.importorskip("pymavlink")

from argos.backends.mavlink.link import SendStatus
from argos.console.control import DO_SET_MODE, FlightControl
from test_console_flight_control import (
    Link, ZERO, ack, heartbeat, landed, profile, simstate,
)


def framing_takeover():
    control, link = FlightControl(enabled=True, framing_enabled=True), Link()
    heartbeat(control)
    simstate(control)
    landed(control)
    token = control.claim(link, 0.)["token"]
    profile(control)
    control.action(token, "prepare", link=link, now=0.)
    heartbeat(control)
    control.action(token, "arm", link=link, now=.01)
    heartbeat(control, .03, armed=True)
    landed(control, .03, 2)
    control.input(token, 0, ZERO, link=link, now=.04)
    frame = {"run_id": "run", "video_id": "camera", "sequence": 1,
             "received_at": .05, "detections": [
                 {"track_id": 7, "confidence": .9, "box": [.45, .4, .1, .2]}]}
    control.framing.observe(frame)
    control.framing.select(7, ("run", "camera"), .05)
    control.framing.engage(.05)
    frame.update(sequence=2, received_at=.1)
    frame["detections"][0]["track_id"] = 8
    control.framing.observe(frame)
    control.tick(link, .1)
    assert control.framing.phase == "takeover"
    assert control.interruption is None
    for seq, now in enumerate((.4, .8, 1.2, 1.6, 2.), 1):
        heartbeat(control, now, armed=True)
        simstate(control, now)
        landed(control, now, 2)
        control.input(token, seq, ZERO, link=link, now=now)
        control.tick(link, now)
    assert control.state(2.)["owned"]
    return control, link, token


@pytest.mark.parametrize("command_failure", ["denied", "timeout", "send_failed"])
def test_framing_timeout_interruption_retains_cause_through_command_errors_and_disarm(command_failure):
    control, link, token = framing_takeover()
    loss = control.framing.last_loss
    if command_failure == "send_failed":
        link.status = SendStatus.BLOCKED
    control.tick(link, 2.1)
    state = control.state(2.1)
    interruption = state["interruption"]
    assert interruption["at"] == 2.1
    assert loss["reason"] in interruption["reason"]
    assert "no manual takeover within 2 seconds" in interruption["reason"]
    assert "Landing requested" in interruption["reason"]
    assert interruption["framing_loss"] == loss == state["framing"]["last_loss"]
    assert not state["owned"] and state["framing"]["phase"] == "idle"
    assert loss["reason"] in state["last_error"]
    land_commands = [msg for msg in link.messages("COMMAND_LONG")
                     if msg.command == DO_SET_MODE and msg.param2 == 9]
    assert len(land_commands) == 1
    if command_failure == "denied":
        ack(control, DO_SET_MODE, 2, 2.2)
    elif command_failure == "timeout":
        control.tick(link, 6.2)
    assert control.state(6.2)["command"]["state"] == command_failure
    assert loss["reason"] in control.state(6.2)["last_error"]
    assert interruption["reason"] in control.state(6.2)["last_error"]
    heartbeat(control, 6.3, armed=False, mode=9)
    landed(control, 6.3, 1)
    with pytest.raises(RuntimeError, match="Control absent"):
        control.input(token, 99, ZERO, link=link, now=6.3)
    control.close(link, 6.4)
    assert control.state(6.4)["interruption"] == interruption
    assert control.state(6.4)["framing"]["last_loss"] == loss


def test_interruption_nested_metadata_is_copied_and_new_claim_resets_it():
    control, link, token = framing_takeover()
    control.tick(link, 2.1)
    expected = control.interruption
    returned = control.state(2.1)
    returned["interruption"]["reason"] = "consumer changed"
    returned["interruption"]["framing_loss"]["detections"][0]["box"][0] = 0.
    returned["framing"]["last_loss"]["detections"].clear()
    control.interruption["framing_loss"]["detections"].clear()
    assert control.interruption == expected
    with pytest.raises(RuntimeError, match="disarming"):
        control.claim(link, 2.2)
    assert control.interruption == expected
    heartbeat(control, 2.3, armed=False, mode=9)
    simstate(control, 2.3)
    landed(control, 2.3, 1)
    result = control.claim(link, 2.3)
    assert result["token"] != token
    assert result["control"]["owned"]
    assert result["control"]["interruption"] is None
    assert result["control"]["framing"]["last_loss"] is None
    assert result["control"]["last_error"] == ""


def test_unowned_failures_do_not_invent_a_lease_interruption():
    control, link = FlightControl(enabled=True), Link()
    control.tick(link, 10.)
    with pytest.raises(RuntimeError):
        control.input("not an owner", 0, ZERO, link=link, now=10.)
    control.close(link, 11.)
    assert control.state(11.)["interruption"] is None


def test_acknowledged_old_framing_loss_is_not_the_cause_of_a_later_browser_expiry():
    control, link, token = framing_takeover()
    historical_loss = control.framing.last_loss
    control.input(token, 10, ZERO | {"forward": .1}, link=link, now=2.02)
    assert control.framing.phase != "takeover"
    assert control.framing.last_loss == historical_loss
    control.tick(link, 2.68)
    interruption = control.interruption
    assert "Browser inputs expired" in interruption["reason"]
    assert interruption["framing_loss"] is None
    assert control.state(2.68)["framing"]["last_loss"] == historical_loss


def test_nonframing_lease_revoke_stores_its_reason_without_inventing_loss():
    control, link = FlightControl(enabled=True), Link()
    heartbeat(control)
    simstate(control)
    token = control.claim(link, 0.)["token"]
    control.action(token, "release", link=link, now=.1)
    assert control.interruption == {"at": .1, "lease_started_at": 0., "reason": "Control released", "framing_loss": None}
