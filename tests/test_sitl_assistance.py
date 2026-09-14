"""Independent authority/loss boundaries of the simulation-only worker."""
from argparse import ArgumentTypeError
from types import SimpleNamespace

import pytest

from argos.backends.mavlink import SendResult, SendStatus
from argos.backends.sitl_assistance import (
    PARAM_MAX_AGE, RELEASE, REQUIRED_PARAMETERS, SitlAssistance, loopback_peer,
)


class Link:
    def __init__(self):
        self.incoming = []
        self.sent = []
        self.closed = False
        self.result = SendResult(SendStatus.ACCEPTED, 42)

    def poll(self, now):
        events, self.incoming = self.incoming, []
        return events

    def send(self, message, now):
        self.sent.append((now, message))
        return self.result

    def close(self):
        self.closed = True


def event(kind, at=10., *, source=(1, 1), **fields):
    return SimpleNamespace(type_name=kind, received_at=at, fields=fields,
                           system=source[0], component=source[1])


def vehicle(worker, at=10., *, armed=True, mode=0, landed=None, switch=1900, source=(1, 1), throttle=1500):
    landed = (2 if armed else 1) if landed is None else landed
    for sample in (
        event("HEARTBEAT", at, source=source, type=2, autopilot=3, base_mode=128 if armed else 0,
              custom_mode=mode, system_status=4, mavlink_version=3),
        event("SIMSTATE", at, source=source, **dict.fromkeys(
            ("roll", "pitch", "yaw", "xacc", "yacc", "zacc", "xgyro", "ygyro", "zgyro", "lat", "lng"), 0.)),
        event("RC_CHANNELS", at, source=source, chancount=16,
              **{f"chan{i}_raw": switch if i == 7 else throttle if i == 3 else 1500 for i in range(1, 8)}),
        event("EXTENDED_SYS_STATE", at, source=source, landed_state=landed),
    ):
        worker.append(sample, at)


def parameters(worker, at=10., *, source=(1, 1)):
    for name, values in REQUIRED_PARAMETERS.items():
        worker.append(event("PARAM_VALUE", at, source=source, param_id=name,
                            param_value=values[0], param_type=9), at)


def observation(at=10., sequence=1, *, identity=7, context=("run-one", "video-one"), box=None, detections=None):
    return {"run_id": context[0], "video_id": context[1], "sequence": sequence,
            "received_at": at, "detections": detections if detections is not None else [
                {"track_id": identity, "box": list(box or (.4, .4, .2, .2)), "confidence": .9}]}


def observe(worker, value, at=None):
    return worker.command({"op": "observe", "observation": value}, value["received_at"] if at is None else at)


@pytest.fixture
def worker():
    value = SitlAssistance(Link())
    vehicle(value)
    parameters(value)
    return value


def engage(worker, at=10.):
    assert observe(worker, observation(at))
    assert worker.command({"op": "engage", "track_id": 7}, at)
    worker.tick(at)
    return worker


def overrides(worker):
    return [message for _, message in worker.link.sent if message.get_type() == "RC_CHANNELS_OVERRIDE"]


def channels(message):
    return [getattr(message, f"chan{i}_raw") for i in range(1, 19)]


@pytest.mark.parametrize("value", ["127.0.0.1:5999", "127.0.0.1:1024", "127.0.0.1:65535"])
def test_only_explicit_loopback_port_is_accepted(value):
    assert loopback_peer(value) == ("127.0.0.1", int(value.rsplit(":", 1)[1]))


@pytest.mark.parametrize("value", ["localhost:5999", "0.0.0.0:5999", "192.168.1.2:5999",
                                   "127.0.0.2:5999", "127.0.0.1:80", "127.0.0.1:65536",
                                   "127.0.0.1:+5999", "127.0.0.1:5.5", "/dev/ttyUSB0", "[::1]:5999"])
def test_other_transports_and_endpoint_spellings_are_rejected(value):
    with pytest.raises(ArgumentTypeError):
        loopback_peer(value)


def test_startup_never_sends_without_recent_correct_simulator_identity():
    worker = SitlAssistance(Link())
    vehicle(worker, source=(2, 1))
    parameters(worker, source=(2, 1))
    worker.tick(10.)
    assert not worker.link.sent
    assert not worker.status(10.)["ready"]
    vehicle(worker)
    worker.tick(10.)
    assert channels(overrides(worker)[0]) == list(RELEASE)
    assert not worker.status(10.)["ready"]  # received parameters from another source do not count
    assert set(message.get_type() for _, message in worker.link.sent) == {
        "RC_CHANNELS_OVERRIDE", "PARAM_REQUEST_READ", "COMMAND_LONG"}


def test_idle_releases_once_reads_profile_and_never_acquires_authority(worker):
    for at in (10., 10.1, 10.2):
        worker.tick(at)
    assert len(overrides(worker)) == 1
    assert channels(overrides(worker)[0]) == list(RELEASE)
    assert worker.status(10.2)["ready"]
    assert not worker.status(10.2)["framing"]["active"]
    requests = [message.param_id for _, message in worker.link.sent if message.get_type() == "PARAM_REQUEST_READ"]
    assert len(set(requests)) == 3


@pytest.mark.parametrize("change,reason", [
    ({"armed": False}, "armed and IN_AIR"), ({"mode": 2}, "Stabilize"),
    ({"landed": 1}, "armed and IN_AIR"), ({"switch": 1500}, "switch is off"),
])
def test_engagement_requires_worker_observed_vehicle_authority(worker, change, reason):
    vehicle(worker, **change)
    observe(worker, observation())
    assert not worker.command({"op": "engage", "track_id": 7}, 10.)
    assert reason in worker.last_rejection["reason"]
    assert not worker.framing.state(10.)["active"]


@pytest.mark.parametrize("bad", [None, {}, {"run_id": "x"}, observation(at=11.), observation(at=9.),
                                 observation(identity=8), observation(detections=[])])
def test_engagement_requires_current_valid_requested_image(worker, bad):
    assert worker.command({"op": "observe", "observation": bad}, 10.)
    assert not worker.command({"op": "engage", "track_id": 7}, 10.)
    assert not worker.framing.state(10.)["active"]


def test_image_law_controls_only_pitch_yaw_with_preserved_mapping(worker):
    engage(worker)
    observe(worker, observation(10.2, 2, box=(.48, .42, .16, .16)))
    worker.tick(10.2)
    axes = worker.framing.axes(10.2)
    assert axes["forward"] > 0 and axes["yaw"] > 0
    assert axes["up"] == axes["right"] == 0
    result = channels(overrides(worker)[-1])
    assert result[1] == round(1500 - 120 * axes["forward"])
    assert result[3] == round(1500 + 120 * axes["yaw"])
    assert all(result[i] == RELEASE[i] for i in range(18) if i not in (1, 3))
    assert worker.status(10.2)["framing"]["profile"] == "pilot_throttle"


def test_pilot_throttle_change_does_not_cancel_or_gain_an_override(worker):
    engage(worker)
    vehicle(worker, 10.1, throttle=1850)
    observe(worker, observation(10.1, 2))
    worker.tick(10.1)
    assert worker.framing.state(10.1)["active"]
    assert channels(overrides(worker)[-1])[2] == 0


def test_immediate_observation_ticks_update_law_without_override_bursts(worker):
    engage(worker)
    for sequence, at in enumerate((10.01, 10.02, 10.03, 10.04), 2):
        observe(worker, observation(at, sequence, box=(.48, .42, .16, .16)))
        worker.tick(at)
        assert worker.framing._last_sequence == sequence
        assert len(overrides(worker)) == 1
    worker.tick(10.05)
    assert len(overrides(worker)) == 2
    expected = worker.framing.axes(10.05)
    assert channels(overrides(worker)[-1])[3] == round(1500 + 120 * expected["yaw"])
    worker.tick(10.05)
    worker.tick(10.051)
    assert len(overrides(worker)) == 2
    status = worker.status(10.051)
    assert status["last_observation_sequence"] == 5
    assert status["last_observation_received_at"] == 10.04
    assert status["last_observation_input_at"] == 10.04


def test_delayed_tick_uses_actual_now_and_does_not_catch_up_old_commands(worker):
    engage(worker)
    observe(worker, observation(10.01, 2, box=(.48, .42, .16, .16)))
    worker.tick(10.50)  # e.g. input decode/other work delayed control servicing
    assert not worker.framing.state(10.5)["active"]
    assert len(overrides(worker)) == 2  # one original command and one release
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    assert worker.framing.state(10.5)["last_loss"]["frame_age_s"] == pytest.approx(.49)


def test_receipt_poll_stall_rechecks_freshness_before_control_send(worker):
    engage(worker)
    observe(worker, observation(10.01, 2))
    worker.clock = lambda: 10.5  # completion time after an unexpectedly slow poll
    worker.tick(10.02)
    assert not worker.framing.state(10.5)["active"]
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    assert worker.last_send_at == 10.5


def test_engagement_does_not_reset_recent_send_rate_limit(worker):
    worker.tick(10.)  # initial all-channel release
    observe(worker, observation(10.01))
    assert worker.command({"op": "engage", "track_id": 7}, 10.01)
    worker.tick(10.01)
    assert len(overrides(worker)) == 1
    worker.tick(10.05)
    assert len(overrides(worker)) == 2


def test_low_high_switch_packets_in_one_poll_latch_takeover(worker):
    engage(worker)
    sample = {f"chan{i}_raw": 1500 for i in range(1, 8)}
    worker.link.incoming = [
        event("RC_CHANNELS", 10.1, chancount=16, **{**sample, "chan7_raw": 1000}),
        event("RC_CHANNELS", 10.1, chancount=16, **{**sample, "chan7_raw": 1900}),
    ]
    worker.tick(10.1)
    assert not worker.framing.state(10.1)["active"]
    assert worker.framing.state(10.1)["target_id"] is None
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    vehicle(worker, 10.2)
    observe(worker, observation(10.2, 2))
    worker.tick(10.2)
    assert not worker.framing.state(10.2)["active"]
    assert worker.command({"op": "engage", "track_id": 7}, 10.2)


def test_stale_image_releases_and_restored_image_requires_explicit_engagement(worker):
    engage(worker)
    vehicle(worker, 10.5)
    worker.tick(10.5)
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    assert worker.framing.state(10.5)["phase"] == "takeover"
    assert "stale" in worker.framing.state(10.5)["last_loss"]["reason"]
    observe(worker, observation(10.6, 2))
    worker.tick(10.6)
    assert not worker.framing.state(10.6)["active"]
    assert worker.command({"op": "engage", "track_id": 7}, 10.6)


def test_unsafe_then_restored_vehicle_reports_in_same_batch_do_not_resume(worker):
    engage(worker)
    for armed in (False, True):
        worker.link.incoming.append(event("HEARTBEAT", 10.1, type=2, autopilot=3,
            base_mode=128 if armed else 0, custom_mode=0, system_status=4, mavlink_version=3))
    worker.tick(10.1)
    assert not worker.framing.state(10.1)["active"]
    assert channels(overrides(worker)[-1]) == list(RELEASE)


def test_short_missing_detection_keeps_only_neutral_attitude_until_same_target_returns(worker):
    engage(worker)
    observe(worker, observation(10.2, 2, detections=[]))
    worker.tick(10.2)
    state = worker.framing.state(10.2)
    assert state["active"] and state["paused"]
    packet = channels(overrides(worker)[-1])
    assert (packet[1], packet[3], packet[2]) == (1500, 1500, 0)
    vehicle(worker, 10.4)
    observe(worker, observation(10.4, 3))
    worker.tick(10.4)
    assert worker.framing.state(10.4)["active"]
    assert not worker.framing.state(10.4)["paused"]


def test_camera_change_cannot_transfer_selection(worker):
    engage(worker)
    observe(worker, observation(10.2, 2, context=("different-run", "different-camera")))
    worker.tick(10.2)
    assert not worker.framing.state(10.2)["active"]
    assert channels(overrides(worker)[-1]) == list(RELEASE)


def test_profile_mismatch_releases_active_assistance_and_blocks_probe(worker):
    engage(worker)
    worker.append(event("PARAM_VALUE", 10.1, param_id="RC_OVERRIDE_TIME", param_value=3.), 10.1)
    worker.tick(10.1)
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    assert not worker.status(10.1)["ready"]
    assert worker.status(10.1)["mismatched_params"] == ["RC_OVERRIDE_TIME"]
    vehicle(worker, 10.2, armed=False)
    assert not worker.command({"op": "probe", "seconds": 1., "pitch": 1530, "yaw": 1540}, 10.2)


def test_parameter_readback_expires_instead_of_becoming_permanent_permission(worker):
    at = 10. + PARAM_MAX_AGE + .01
    vehicle(worker, at)
    assert not worker.status(at)["ready"]
    assert set(worker.status(at)["missing_params"]) == set(REQUIRED_PARAMETERS)


@pytest.mark.parametrize("name", ["ARMING_SKIPCHK", "GPS1_TYPE", "GPS2_TYPE", "MNT1_TYPE",
                                  "SERVO9_FUNCTION", "SERVO10_FUNCTION", "SERVO11_FUNCTION"])
def test_arming_gps_and_camera_parameters_require_actual_matching_readback(worker, name):
    del worker._params[name]
    assert name in worker.status(10.)["missing_params"]
    observe(worker, observation())
    assert not worker.command({"op": "engage", "track_id": 7}, 10.)
    worker.append(event("PARAM_VALUE", param_id=name, param_value=1.), 10.)
    assert name in worker.status(10.)["mismatched_params"]
    worker.append(event("PARAM_VALUE", param_id=name, param_value=0.), 10.)
    assert worker.status(10.)["ready"]


def test_extended_state_request_is_simulator_guarded_bounded_and_not_a_receipt():
    worker = SitlAssistance(Link())
    worker.tick(10.)
    assert not worker.link.sent
    vehicle(worker)
    parameters(worker)
    worker._landed_at = None
    worker.tick(10.)
    requests = lambda: [message for _, message in worker.link.sent if message.get_type() == "COMMAND_LONG"]
    assert len(requests()) == 1
    message = requests()[0]
    assert (message.command, message.param1, message.param2) == (511, 245., 200000.)
    assert (message.target_system, message.target_component) == (1, 1)
    worker.append(event("COMMAND_ACK", 10.1, command=511, result=0), 10.1)
    worker.tick(10.1)
    assert len(requests()) == 1
    assert not worker.status(10.1)["ready"]
    worker.tick(11.9)
    assert len(requests()) == 1
    worker.tick(12.)
    assert len(requests()) == 2
    vehicle(worker, 12.1)
    worker.tick(12.1)
    assert worker.status(12.1)["ready"]
    worker.tick(14.1)
    assert len(requests()) == 2  # no repeat while actual landed-state receipt is recent
    worker.tick(16.)
    assert len(requests()) == 2  # stale simulator identity forbids requests too


def test_receiver_receipt_expiry_releases_assistance(worker):
    engage(worker)
    observe(worker, observation(10.7, 2))
    worker.tick(10.7)
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    assert not worker.framing.state(10.7)["active"]
    assert "RC_CHANNELS" in worker.status(10.7)["reason"]


def test_expired_simulator_identity_sends_nothing_and_does_not_restore_authority(worker):
    engage(worker)
    sent = len(worker.link.sent)
    worker.tick(14.)
    assert len(worker.link.sent) == sent
    assert not worker.framing.state(14.)["active"]
    vehicle(worker, 14.1)
    observe(worker, observation(14.1, 2))
    worker.tick(14.1)
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    assert not worker.framing.state(14.1)["active"]


def test_ground_probe_continues_with_switch_low_then_releases_at_absolute_deadline(worker):
    events = []
    worker.emit = events.append
    vehicle(worker, armed=False, switch=1000)
    assert worker.command({"op": "probe", "seconds": 2., "pitch": 1530, "yaw": 1540}, 10.)
    for at in (10., 10.5, 11., 11.5):
        vehicle(worker, at, armed=False, switch=1000)
        worker.tick(at)
        packet = channels(overrides(worker)[-1])
        assert (packet[1], packet[3]) == (1530, 1540)
        assert all(packet[i] == RELEASE[i] for i in range(18) if i not in (1, 3))
        assert worker.status(at)["probe_active"]
    vehicle(worker, 12., armed=False, switch=1000)
    worker.tick(12.)
    assert not worker.status(12.)["probe_active"]
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    assert any(item["event"] == "ground_probe_started" for item in events)


@pytest.mark.parametrize("change", [{"armed": True}, {"armed": False, "landed": 2}, {"armed": False, "mode": 2}])
def test_probe_rejects_airborne_armed_or_non_stabilize_vehicle(worker, change):
    vehicle(worker, **change)
    assert not worker.command({"op": "probe", "seconds": 1., "pitch": 1530, "yaw": 1540}, 10.)


@pytest.mark.parametrize("change", [{"seconds": 0}, {"seconds": 2.01}, {"seconds": True}, {"seconds": float("nan")},
                                    {"pitch": 1561}, {"yaw": 1439}, {"pitch": 1530.},
                                    {"pitch": 1500, "yaw": 1500}])
def test_probe_rejects_unbounded_or_non_numeric_demands(worker, change):
    vehicle(worker, armed=False)
    command = {"op": "probe", "seconds": 1., "pitch": 1530, "yaw": 1540, **change}
    assert not worker.command(command, 10.)
    assert not worker.status(10.)["probe_active"]


def test_probe_stops_if_vehicle_arms(worker):
    vehicle(worker, armed=False)
    assert worker.command({"op": "probe", "seconds": 2., "pitch": 1530, "yaw": 1540}, 10.)
    worker.tick(10.)
    vehicle(worker, 10.1, armed=True)
    worker.tick(10.1)
    assert not worker.status(10.1)["probe_active"]
    assert channels(overrides(worker)[-1]) == list(RELEASE)


def test_explicit_stop_and_process_recreation_never_resume_framing(worker):
    engage(worker)
    assert worker.command({"op": "stop"}, 10.1)
    worker.tick(10.1)
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    replacement = SitlAssistance(Link())
    vehicle(replacement, 10.2)
    parameters(replacement, 10.2)
    observe(replacement, observation(10.2))
    replacement.tick(10.2)
    assert not replacement.framing.state(10.2)["active"]
    assert channels(overrides(replacement)[-1]) == list(RELEASE)


def test_failed_transport_drops_authority_without_retries(worker):
    engage(worker)
    worker.link.result = SendResult(SendStatus.PARTIAL, 12, "short write")
    worker.tick(10.1)
    assert worker.link.closed
    assert not worker.status(10.1)["ready"]
    assert not worker.framing.state(10.1)["active"]
    count = len(worker.link.sent)
    worker.tick(10.2)
    assert len(worker.link.sent) == count


def test_orderly_shutdown_releases_without_heartbeat_arm_or_mode_commands(worker):
    engage(worker)
    worker.close(10.1)
    assert worker.link.closed
    assert channels(overrides(worker)[-1]) == list(RELEASE)
    assert set(message.get_type() for _, message in worker.link.sent) == {
        "PARAM_REQUEST_READ", "RC_CHANNELS_OVERRIDE", "COMMAND_LONG"}
    commands = [message for _, message in worker.link.sent if message.get_type() == "COMMAND_LONG"]
    assert all((message.command, message.param1) == (511, 245.) for message in commands)


@pytest.mark.parametrize("values", [{"op": "engage", "track_id": True}, {"op": "stop", "throttle": 0},
                                   {"op": "arm"}, None, {"op": []}])
def test_commands_reject_extra_authority_and_invalid_schemas(worker, values):
    assert not worker.command(values, 10.)
    assert worker.last_rejection["kind"] == "rejected"


def test_status_has_bounded_external_observer_contract(worker):
    engage(worker)
    state = worker.status(10.)
    assert {"kind", "at", "ready", "reason", "framing", "probe_active", "sent_count",
            "last_override", "last_send_at"} <= state.keys()
    assert state["kind"] == "status" and state["at"] == 10.
    assert state["sent_count"] == 1
    assert len(state["last_override"]) == 18
