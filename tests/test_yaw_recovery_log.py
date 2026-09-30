"""Every recovery metric is queued, with visible bounds and separate disk work."""
import json
import threading
import time

import pytest

from argos.console.yaw_assist import YawAssistService


def records(directory):
    return [json.loads(line) for line in (directory / "recovery.jsonl").read_text().splitlines()]


def test_burst_retains_every_attempt_in_order_and_preserves_metric_content(tmp_path):
    service = YawAssistService("/dev/test", 8080, directory=tmp_path, clock=lambda: 14.5)
    events = [{"attempt": i, "decision": "rejected" if i % 2 else "accepted",
               "candidate_id": i + 10, "cosine": .83, "iou": .42,
               "center_distance": .18, "geometry": {"area_ratio": .91}}
              for i in range(160)]
    try:
        for event in events:
            assert service.record_recovery(event)
        # The callback must own a copy, including nested metric values.
        events[0]["geometry"]["area_ratio"] = 0
    finally:
        service.close()
    actual = records(tmp_path)
    assert [item["event"]["attempt"] for item in actual] == list(range(160))
    assert all(item["monotonic_at"] == 14.5 for item in actual)
    assert actual[0]["event"]["geometry"]["area_ratio"] == .91
    assert actual[7]["event"] == events[7]
    counters = service.snapshot()["recovery_log"]
    assert counters["queued"] == counters["written"] == 160
    assert counters["dropped"] == counters["failed"] == counters["pending"] == 0
    assert not counters["shutdown_incomplete"]
    summary = json.loads((tmp_path / "recovery-status.json").read_text())
    assert summary["written"] == 160 and summary["pending"] == 0


def test_stalled_recovery_disk_does_not_block_callback_or_radio_status(tmp_path):
    entered, release = threading.Event(), threading.Event()
    service = YawAssistService("/dev/test", 8080, directory=tmp_path)
    write = service._write_recovery_log
    caller = threading.get_ident()

    def blocked(items):
        assert threading.get_ident() != caller
        entered.set()
        release.wait(2)
        write(items)

    service._write_recovery_log = blocked
    try:
        assert service.record_recovery({"attempt": 0})
        assert entered.wait(1)
        started = time.monotonic()
        for i in range(1, 101):
            assert service.record_recovery({"attempt": i})
        service._publish({"connected": True, "radio_state": "A", "reason": "active"})
        assert time.monotonic() - started < .2
        assert service.snapshot()["radio_state"] == "A"
        assert service.snapshot()["recovery_log"]["pending"] == 101
    finally:
        release.set()
        service.close()
    assert len(records(tmp_path)) == 101


def test_equal_attempt_metrics_are_not_deduplicated_or_time_sampled(tmp_path):
    service = YawAssistService("/dev/test", 8080, directory=tmp_path, clock=lambda: 10.)
    event = {"decision": "rejected", "cosine": .6, "iou": .2, "reason": "appearance"}
    for _ in range(8):
        assert service.record_recovery(event)
    service.close()
    assert records(tmp_path) == [{"monotonic_at": 10., "event": event}] * 8


def test_overflow_is_explicit_in_live_and_persisted_counters(tmp_path):
    entered, release = threading.Event(), threading.Event()
    service = YawAssistService("/dev/test", 8080, directory=tmp_path, recovery_log_capacity=3)
    write = service._write_recovery_log

    def blocked(items):
        entered.set()
        release.wait(2)
        write(items)

    service._write_recovery_log = blocked
    try:
        assert service.record_recovery({"attempt": 0})
        assert entered.wait(1)
        assert service.record_recovery({"attempt": 1})
        assert service.record_recovery({"attempt": 2})
        assert not service.record_recovery({"attempt": 3})
        snapshot = service.snapshot()
        assert snapshot["recovery_log"]["pending"] == 3
        assert snapshot["recovery_log"]["dropped"] == 1
        assert "queue is full" in snapshot["log_error"]
    finally:
        release.set()
        service.close()
    assert [item["event"]["attempt"] for item in records(tmp_path)] == [0, 1, 2]
    summary = json.loads((tmp_path / "recovery-status.json").read_text())
    assert summary["queued"] == summary["written"] == 3
    assert summary["dropped"] == 1 and "not recorded" in summary["log_error"]


def test_shutdown_is_bounded_and_reports_incomplete_flush_while_disk_is_stuck(tmp_path):
    entered, release = threading.Event(), threading.Event()
    service = YawAssistService("/dev/test", 8080, directory=tmp_path)
    write = service._write_recovery_log

    def blocked(items):
        entered.set()
        release.wait(2)
        write(items)

    service._write_recovery_log = blocked
    try:
        service.record_recovery({"attempt": 0})
        assert entered.wait(1)
        service.record_recovery({"attempt": 1})
        started = time.monotonic()
        service.close()
        assert time.monotonic() - started < .5
        snapshot = service.snapshot()["recovery_log"]
        assert snapshot["shutdown_incomplete"] and snapshot["pending"] == 2
    finally:
        release.set()
        service._log_thread.join(timeout=1)
    assert not service._log_thread.is_alive()
    assert len(records(tmp_path)) == 2
    assert not service.snapshot()["recovery_log"]["shutdown_incomplete"]


def test_disk_errors_do_not_kill_logger_or_silently_lose_attempts(tmp_path):
    service = YawAssistService("/dev/test", 8080, directory=tmp_path)

    def failed(items):
        raise OSError("disk is unavailable")

    service._write_recovery_log = failed
    for i in range(7):
        service.record_recovery({"attempt": i})
    service.close()
    counters = service.snapshot()["recovery_log"]
    assert counters["queued"] == counters["failed"] == 7
    assert counters["written"] == counters["dropped"] == counters["pending"] == 0
    assert "disk is unavailable" in service.snapshot()["log_error"]
    summary = json.loads((tmp_path / "recovery-status.json").read_text())
    assert summary["failed"] == 7 and "disk is unavailable" in summary["log_error"]


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_recovery_queue_capacity_must_be_bounded_positive_integer(capacity):
    with pytest.raises(ValueError, match="capacity"):
        YawAssistService("/dev/test", 8080, recovery_log_capacity=capacity)
