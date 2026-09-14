"""Receiver evidence must distinguish firmware rejection from cooperative release."""
import pytest

from examples.radio_bench_scenario import (
    BenchFailure, prove_camera_correction, prove_disengaged, prove_framing_interval, prove_switch_rejection,
    receiver_matches, require_live_probe,
)


def receipts():
    return [{"at": at, "fields": {"time_boot_ms": boot, "chan4_raw": 1420, "chan7_raw": 1100}}
            for at, boot in ((10.1, 1000), (10.25, 1100), (10.4, 1200))]


def sends():
    channels = [0, 1530, 0, 1540, 0, 0, 0, 0] + [65534] * 10
    return [{"probe_active": True, "last_send_at": at, "last_override": channels}
            for at in (10.15, 10.3)]


def prove(rx=None, tx=None, **kwargs):
    return prove_switch_rejection(receipts() if rx is None else rx, sends() if tx is None else tx,
                                  switch_at=10., pilot_yaw=1420, override_yaw=1540, **kwargs)


def test_distinct_conflicting_send_and_receiver_evidence_is_required():
    result = prove()
    assert result["receiver_samples"] == 3
    assert result["conflicting_send_samples"] == 2
    assert result["pilot_yaw"] != result["override_yaw"]


@pytest.mark.parametrize("tx", [[], [{"probe_active": False}],
    [{"probe_active": True, "last_send_at": 9., "last_override": [0] * 18}],
    [{"probe_active": True, "last_send_at": 10.2, "last_override": [0] * 18}]])
def test_cooperative_release_or_old_sends_do_not_prove_firmware_rejection(tx):
    with pytest.raises(BenchFailure, match="overlapping"):
        prove(tx=tx)


def test_repeated_status_for_one_send_is_not_multiple_transmissions():
    with pytest.raises(BenchFailure, match="overlapping"):
        prove(tx=[sends()[0]] * 4)


def test_repeated_receiver_frame_is_not_distinct_firmware_evidence():
    with pytest.raises(BenchFailure, match="distinct receiver"):
        prove(rx=[receipts()[0]] * 4)


def test_old_receiver_evidence_does_not_satisfy_new_switch_action():
    rx = receipts()
    for entry in rx:
        entry["at"] -= 1
    with pytest.raises(BenchFailure, match="distinct receiver"):
        prove(rx=rx)


def test_switch_still_high_does_not_prove_rejection():
    rx = receipts()
    for entry in rx:
        entry["fields"]["chan7_raw"] = 1900
    with pytest.raises(BenchFailure, match="distinct receiver"):
        prove(rx=rx)


def test_identical_pilot_and_assistance_stimuli_are_uninformative():
    with pytest.raises(BenchFailure, match="must differ"):
        prove_switch_rejection(receipts(), sends(), switch_at=10., pilot_yaw=1540, override_yaw=1540)


@pytest.mark.parametrize("fields", [{}, {"chan3_raw": None}, {"chan3_raw": 1500.}, {"chan3_raw": True}])
def test_missing_or_invalid_receiver_data_cannot_be_inferred_from_request(fields):
    assert not receiver_matches(fields, {3: 1500})


def test_exact_observed_channels_are_required():
    assert receiver_matches({"chan3_raw": 1480, "chan7_raw": 1900}, {3: 1480, 7: 1900})
    assert not receiver_matches({"chan3_raw": 1500, "chan7_raw": 1900}, {3: 1480, 7: 1900})


def test_an_interleaved_override_receipt_cannot_be_filtered_out():
    rx = receipts()
    rx.insert(2, {"at": 10.3, "fields": {"time_boot_ms": 1150, "chan4_raw": 1540, "chan7_raw": 1100}})
    with pytest.raises(BenchFailure, match="distinct receiver"):
        prove(rx=rx)


@pytest.mark.parametrize("patch", [{"probe_active": False}, {"probe_remaining_s": .1},
    {"at": 8.}, {"last_send_at": 8.}, {"last_override": [0] * 18}])
def test_a_naturally_expired_or_stale_probe_cannot_be_used_for_kill_evidence(patch):
    status = {**sends()[0], "at": 10.15, "probe_remaining_s": 1.2, **patch}
    with pytest.raises(BenchFailure, match="still-active probe"):
        require_live_probe(status, 10.2)


def framing_states():
    return [{"at": at, "last_send_at": at - .01,
             "last_override": [0, 1501, 0, 1504, 0, 0, 0, 0] + [65534] * 10,
             "framing": {"active": True, "paused": False}}
            for at in (10., 10.1, 10.2, 10.3, 10.4)]


def test_framing_interval_requires_continuous_fresh_sends():
    result = prove_framing_interval(framing_states(), start=10., end=10.45)
    assert result["distinct_send_samples"] == 5


def test_a_recovered_pause_inside_throttle_step_is_still_a_failure():
    states = framing_states()
    states[2]["framing"]["paused"] = True
    with pytest.raises(BenchFailure, match="paused"):
        prove_framing_interval(states, start=10., end=10.45)


def test_frozen_worker_status_cannot_pass_throttle_step():
    states = framing_states()
    for sample in states:
        sample["last_send_at"] = 9.9
    with pytest.raises(BenchFailure):
        prove_framing_interval(states, start=10., end=10.45)


def test_transient_resume_cannot_hide_behind_final_idle_status():
    states = framing_states()
    for state in states:
        state["probe_active"] = False
        state["framing"]["active"] = False
    assert prove_disengaged(states, start=10., end=10.45)
    states[2]["framing"]["active"] = True
    with pytest.raises(BenchFailure, match="resumed"):
        prove_disengaged(states, start=10., end=10.45)


def test_camera_correction_needs_actual_readback_not_just_announced_send():
    states = framing_states()
    rx = [{"at": 10.12, "fields": {"time_boot_ms": 1000, "chan4_raw": 1504, "chan7_raw": 1900}}]
    result = prove_camera_correction(states, rx, start=10., end=10.45)
    assert result["channel"] == 4 and result["pwm"] == 1504
    rx[0]["fields"]["chan4_raw"] = 1500
    with pytest.raises(BenchFailure, match="No non-neutral"):
        prove_camera_correction(states, rx, start=10., end=10.45)


def test_old_or_neutral_image_corrections_do_not_prove_acceptance():
    states = framing_states()
    rx = [{"at": 10.12, "fields": {"chan4_raw": 1504, "chan7_raw": 1900}}]
    for state in states:
        state["last_send_at"] = 9.
    with pytest.raises(BenchFailure):
        prove_camera_correction(states, rx, start=10., end=10.45)


def closing_scenario(tmp_path, monkeypatch, visual_states):
    import io
    import json
    from examples import radio_bench_scenario as bench

    scenario = bench.Scenario(console_url="http://127.0.0.1:8083",
        mavlink_peer=("127.0.0.1", 5964), assistance_peer=("127.0.0.1", 5962),
        rc_peer=("127.0.0.1", 5521), output_dir=tmp_path, python="python", env={})
    scenario.report["passed"] = True
    scenario.recording = {"id": "capture"}
    statuses = iter(visual_states)

    def http(route, value=None, **kwargs):
        visual = {"id": "capture", "frames": 10, "samples": 20, **next(statuses)}
        status = {"id": "capture", "state": "complete", "visual": visual}
        return io.StringIO(json.dumps(status if route.endswith("/stop") else {"recording": status}))

    monkeypatch.setattr(scenario, "http", http)
    monkeypatch.setattr(bench.time, "sleep", lambda seconds: None)
    return scenario


def test_report_waits_for_visual_finalization(tmp_path, monkeypatch):
    scenario = closing_scenario(tmp_path, monkeypatch,
        [{"state": "finalizing"}, {"state": "complete", "frames": 11}])
    scenario.close()
    assert scenario.report["passed"]
    assert scenario.report["recording_final"]["visual"]["frames"] == 11
    assert scenario.report["recording_final"]["visual"]["state"] == "complete"


@pytest.mark.parametrize("final", [{"state": "error"}, {"state": "complete", "id": "different"},
                                  {"state": "complete", "frames": 0}])
def test_late_visual_failure_or_replacement_cannot_pass(tmp_path, monkeypatch, final):
    scenario = closing_scenario(tmp_path, monkeypatch, [{"state": "finalizing"}, final])
    scenario.close()
    assert not scenario.report["passed"]
    assert scenario.report["recording_error"]


def test_one_cleanup_failure_does_not_skip_other_children_or_capture(tmp_path, monkeypatch):
    from types import SimpleNamespace
    scenario = closing_scenario(tmp_path, monkeypatch, [{"state": "complete"}])
    closed = []

    def failed_close():
        raise OSError("injected cleanup failure")

    scenario.children = [SimpleNamespace(name="first", close=lambda: closed.append("first")),
                         SimpleNamespace(name="second", close=failed_close)]
    scenario.close()
    assert closed == ["first"]
    assert not scenario.report["passed"]
    assert "second" in scenario.report["cleanup_errors"][0]
    assert scenario.report["recording_final"]["visual"]["state"] == "complete"
