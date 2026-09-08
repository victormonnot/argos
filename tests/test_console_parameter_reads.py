"""Parameter discovery is paced, bounded and separate from flight actions."""
import pytest

mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")

from argos.backends.mavlink.link import SendResult, SendStatus
from argos.console.control import (
    FlightControl, PARAMETER_INITIAL_BATCH, PARAMETER_READ_INTERVAL,
    PARAMETER_READ_WINDOW, REQUIRED_PARAMETERS,
)
from test_console_flight_control import (
    Link, ZERO, event, heartbeat, landed, profile, simstate,
)


def claimed(*, framing=True, now=0.):
    control, link = FlightControl(enabled=True, framing_enabled=framing), Link()
    heartbeat(control, now)
    simstate(control, now)
    landed(control, now)
    token = control.claim(link, now)["token"]
    return control, link, token


def receipt(control, key, value, now, *, received_at=None):
    control.append(event(mav.MAVLink_param_value_message(
        key.encode("ascii"), value, 9, 45, 0),
        now if received_at is None else received_at), now=now)


def step(control, link, token, now, *, armed=False):
    heartbeat(control, now, armed=armed)
    simstate(control, now)
    control.input(token, control.state(now)["input_seq"] + 1, ZERO, link=link, now=now)
    control.tick(link, now)


def reads(link):
    return [(message.param_id, at) for message, at in link.sent
            if message.get_type() == "PARAM_REQUEST_READ"]


@pytest.mark.parametrize("framing", [False, True])
def test_initial_batch_is_legacy_size_then_only_one_read_per_interval(framing):
    control, link, token = claimed(framing=framing)
    assert [key for key, _ in reads(link)] == list(REQUIRED_PARAMETERS)
    assert len(reads(link)) == PARAMETER_INITIAL_BATCH == 17
    control.tick(link, .099)
    assert len(reads(link)) == 17
    step(control, link, token, .1)
    assert len(reads(link)) == 18
    control.tick(link, .1)
    control.tick(link, .199)
    assert len(reads(link)) == 18
    step(control, link, token, .2)
    assert len(reads(link)) == 19
    assert all(message.param_index == -1 for message in link.messages("PARAM_REQUEST_READ"))
    assert not link.messages("PARAM_SET")


def test_framing_profile_completes_after_preparation_with_a_bounded_response_queue():
    control, link, token = claimed()
    expected = control.state(0.)["profile"]["required"]
    delivered = 0

    def drain(now):
        nonlocal delivered
        pending = reads(link)[delivered:]
        # Model the observed finite autopilot response queue: a burst beyond 20
        # would lose its tail. Paced requests leave room for every response.
        for key, _ in pending[:20]:
            receipt(control, key, expected[key], now)
        delivered += len(pending)

    drain(.01)
    control.action(token, "prepare", link=link, now=.01)
    heartbeat(control, .02)
    assert control.state(.02)["prepared"]
    assert not control.state(.02)["profile"]["ready"]
    for index in range(1, 31):
        now = index * .11
        step(control, link, token, now)
        drain(now)
    assert control.state(now)["profile"]["ready"]
    assert len(reads(link)) == len(expected) == 45
    assert {key for key, _ in reads(link)} == set(expected)
    control.action(token, "arm", link=link, now=now)
    assert control.state(now)["command"]["action"] == "arm"
    assert len([message for message in link.messages("COMMAND_LONG")
                if message.command == 400]) == 1


def test_reads_skip_valid_receipts_but_retry_missing_initial_responses():
    control, link, token = claimed()
    expected = control.state(0.)["profile"]["required"]
    missing = "GPS1_TYPE"
    for key, value in expected.items():
        if key != missing:
            # A received mismatch is evidence to reject arming, not a missing
            # response which should be repeatedly requested until it changes.
            receipt(control, key, 1. if key == "GPS2_TYPE" else value, .01)
    step(control, link, token, .1)
    step(control, link, token, .21)
    assert reads(link)[17:] == [(missing, .1), (missing, .21)]
    receipt(control, missing, expected[missing], .22)
    step(control, link, token, .32)
    assert len(reads(link)) == 19
    assert control.state(.32)["profile"]["mismatched"] == ["GPS2_TYPE"]


@pytest.mark.parametrize("value,received_at", [(float("nan"), 1.01), (0., .99)])
def test_invalid_or_previous_lease_receipt_does_not_suppress_missing_read(value, received_at):
    control, link, token = claimed(now=1.)
    for key, expected in control.state(1.)["profile"]["required"].items():
        if key != "GPS1_TYPE":
            receipt(control, key, expected, 1.01)
    receipt(control, "GPS1_TYPE", value, 1.01, received_at=received_at)
    step(control, link, token, 1.11)
    assert reads(link)[17:] == [("GPS1_TYPE", 1.11)]


def test_retry_window_is_absolute_despite_neutral_renewal_and_missing_responses():
    control, link, token = claimed()
    for index in range(1, 321):
        step(control, link, token, index * .05)
    actual = reads(link)
    later = [at for _, at in actual[17:]]
    assert later and max(later) < PARAMETER_READ_WINDOW
    assert all(right - left >= PARAMETER_READ_INTERVAL - 1e-10
               for left, right in zip(later, later[1:]))
    assert len(later) <= int(PARAMETER_READ_WINDOW / PARAMETER_READ_INTERVAL)
    assert control.state(16.)["owned"]
    assert control.state(16.)["profile"]["missing"]
    # Only the original stream requests exist; retries never issue mode/arm.
    assert [message.command for message in link.messages("COMMAND_LONG")] == [511, 511]
    step(control, link, token, 16.1)
    assert reads(link) == actual


def test_late_tick_sends_one_missing_read_without_catching_up_a_burst():
    control, link, token = claimed()
    control.input(token, 0, ZERO, link=link, now=.5)
    control.tick(link, .5)
    assert len(reads(link)) == 18
    control.tick(link, .501)
    assert len(reads(link)) == 18


def test_armed_unknown_or_uncertain_arm_blocks_parameter_reads():
    for guard in ("armed", "unknown", "uncertain"):
        control, link, token = claimed()
        if guard == "armed":
            # A full profile permits a real arm; retain missing queue entries to
            # verify the scheduler's flight boundary independently of filtering.
            profile(control)
            heartbeat(control, .01, armed=True)
        elif guard == "unknown":
            control._armed = None
        else:
            control._arm_uncertain = True
        control.input(token, 0, ZERO, link=link, now=.1)
        control.tick(link, .1)
        assert len(reads(link)) == 17


def test_revoke_clears_reads_and_reclaim_starts_fresh_queue_and_deadline():
    control, link, token = claimed()
    step(control, link, token, .1)
    control.action(token, "release", link=link, now=.2)
    count = len(reads(link))
    control.tick(link, 10.)
    assert len(reads(link)) == count
    heartbeat(control, 10.)
    simstate(control, 10.)
    new_token = control.claim(link, 10.)["token"]
    assert new_token != token
    assert len(reads(link)) == count + 17
    step(control, link, new_token, 10.11)
    assert reads(link)[-1][0] == "RCMAP_ROLL"
    for index in range(1, 32):
        step(control, link, new_token, 10.11 + index * .2)
    assert reads(link)[-1][1] > 15.  # new lease, new absolute deadline


def test_failed_paced_read_revokes_without_retrying_the_send():
    control, link, token = claimed()
    original_send = link.send

    def fail_reads(message, now):
        if message.get_type() == "PARAM_REQUEST_READ":
            link.sent.append((message, now))
            return SendResult(SendStatus.BLOCKED, 0)
        return original_send(message, now)

    link.send = fail_reads
    step(control, link, token, .1)
    assert not control.state(.1)["owned"]
    assert control.state(.1)["phase"] == "error"
    assert "Parameter read" in control.state(.1)["last_error"]
    count = len(link.sent)
    control.tick(link, .21)
    assert len(link.sent) == count
    assert not any(message.command in (176, 400)
                   for message in link.messages("COMMAND_LONG"))
