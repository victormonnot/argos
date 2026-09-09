"""Framing's digital input map is explicit and checked throughout its lease."""
from pathlib import Path

import pytest

mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")

from argos.console.control import (
    DIGITAL_INPUT_PARAMETERS, FRAMING_PARAMETERS, PARAMETER_INITIAL_BATCH,
    REQUIRED_PARAMETERS, FlightControl,
)
from test_console_flight_control import Link, event, heartbeat, landed, simstate


def parameter(control, key, value, at=0., *, received_at=None):
    receipt = at if received_at is None else received_at
    control.append(event(mav.MAVLink_param_value_message(
        key.encode("ascii"), value, 9, 100, 0), receipt), now=at)


def claimed(*, framing=True):
    control, link = FlightControl(enabled=True, framing_enabled=framing), Link()
    heartbeat(control)
    simstate(control)
    landed(control)
    token = control.claim(link, 0.)["token"]
    return control, link, token


def confirmed_profile(control, **overrides):
    for key, expected in control.state(0.)["profile"]["required"].items():
        parameter(control, key, overrides.get(key, expected))


@pytest.mark.parametrize("framing", [False, True])
def test_extra_parameter_requests_and_checks_require_explicit_framing_enablement(framing):
    control, link, _ = claimed(framing=framing)
    expected = REQUIRED_PARAMETERS | (FRAMING_PARAMETERS if framing else {})
    # Both profiles retain the small initial batch; the framing additions are
    # read over later ticks so they do not saturate the autopilot response queue.
    assert {message.param_id for message in link.messages("PARAM_REQUEST_READ")} == set(
        list(REQUIRED_PARAMETERS)[:PARAMETER_INITIAL_BATCH])
    assert control.state(0.)["profile"]["required"] == expected
    assert DIGITAL_INPUT_PARAMETERS.items() <= expected.items()
    assert not set(FRAMING_PARAMETERS) & set(REQUIRED_PARAMETERS)
    confirmed_profile(control)
    assert control.state(0.)["profile"]["ready"]
    assert not link.messages("PARAM_SET")
    if not framing:
        parameter(control, "MNT1_TYPE", 1, .01)
        assert "MNT1_TYPE" not in control.state(.01)["profile"]["values"]
        assert control.state(.01)["profile"]["ready"]
        # Manual flight needs the same neutral digital attitude map even when
        # there is no camera law. Only camera-specific additions remain opt-in.
        parameter(control, "RCMAP_YAW", 8, .02)
        assert control.state(.02)["profile"]["mismatched"] == ["RCMAP_YAW"]


@pytest.mark.parametrize("key,bad", [
    ("RCMAP_YAW", 8), ("RC2_REVERSED", 1), ("RC2_TRIM", 1450),
    ("RC4_MAX", 2000), ("RC3_MIN", 1900), ("SIMPLE", 1),
    ("PILOT_Y_RATE", 360), ("PILOT_Y_EXPO", -.5), ("MNT1_TYPE", 1),
    ("SERVO10_FUNCTION", 7),
])
def test_changed_mapping_calibration_or_camera_profile_prevents_arming(key, bad):
    control, link, token = claimed()
    confirmed_profile(control, **{key: bad})
    control.action(token, "prepare", link=link, now=0.)
    heartbeat(control, mode=2)
    with pytest.raises(RuntimeError, match="profile"):
        control.action(token, "arm", link=link, now=.01)
    assert control.state(.01)["profile"]["mismatched"] == [key]
    assert not any(message.command == 400 for message in link.messages("COMMAND_LONG"))


def test_parameter_change_during_active_framing_uses_existing_landing_path():
    control, link, token = claimed()
    confirmed_profile(control)
    control.action(token, "prepare", link=link, now=0.)
    heartbeat(control, mode=2)
    control.action(token, "arm", link=link, now=.01)
    heartbeat(control, .02, armed=True, mode=2)
    landed(control, .02, 2)
    control.framing.observe({
        "run_id": "run", "video_id": "camera", "sequence": 1, "received_at": .03,
        "detections": [{"track_id": 1, "box": [.45, .4, .1, .2], "confidence": .9}],
    })
    control.framing.select(1, ("run", "camera"), .03)
    control.framing.engage(.03)
    assert control.framing.phase == "active"
    parameter(control, "RC2_REVERSED", 1, .04)
    control.tick(link, .05)
    assert not control.state(.05)["owned"]
    assert control.framing.phase != "active"
    assert control.state(.05)["command"]["action"] == "land"
    assert "profile changed" in control.state(.05)["last_error"]
    before = len(link.sent)
    control.tick(link, .2)
    assert len(link.sent) == before


def test_new_lease_does_not_reuse_old_framing_parameter_evidence():
    control, link, token = claimed()
    confirmed_profile(control)
    control.action(token, "release", link=link, now=.1)
    control.claim(link, .2)
    parameter(control, "PILOT_Y_RATE", 202.5, .21, received_at=.1)
    assert "PILOT_Y_RATE" in control.state(.21)["profile"]["missing"]
    parameter(control, "PILOT_Y_RATE", 202.5, .22)
    assert "PILOT_Y_RATE" not in control.state(.22)["profile"]["missing"]


def test_launcher_profile_explicitly_defines_every_checked_value_once():
    path = Path(__file__).parents[1] / "examples/sitl-web-control.parm"
    pairs = [line.split() for line in path.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    values = {key: float(value) for key, value in pairs}
    assert len(values) == len(pairs)
    for key, expected in (REQUIRED_PARAMETERS | FRAMING_PARAMETERS).items():
        assert values[key] == expected
