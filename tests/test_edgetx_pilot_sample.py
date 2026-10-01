"""Pilot observations cannot manufacture command acceptance or live authority."""
from dataclasses import replace

import pytest

from argos.backends.edgetx_distance_stream import RadioStatus
from argos.backends.edgetx_pilot_sample import PilotLines, PilotObserver, parse_pilot_sample
from argos.backends.edgetx_probe import ProbeError
from argos.backends.edgetx_yaw_stream import YawStream, SourceSample, RadioStatus as YawStatus
from argos.backends.edgetx_distance_stream import DistanceStream


LINE = b"AP1 1234abcd 2 3 4 AADAA11 -1024 23 1000 -20 -205 51"
STATUS = RadioStatus("1234abcd", 2, 3, 4, "A", "A", "D", "A", "A")


def observe(observer, line=LINE, *, status=STATUS, at=100.):
    observer.observe(line, status=status, session="1234abcd", pending_begin=False, received_at=at)


def test_pilot_values_are_pre_mix_and_distinct_from_the_current_lua_outputs():
    sample = parse_pilot_sample(LINE)
    assert sample["sticks"] == {"roll": -1024, "pitch": 23, "throttle": 1000, "yaw": -20}
    assert sample["lua_outputs"] == {"yaw": {"valid": True, "value": -205},
                                     "pitch": {"valid": True, "value": 51}}
    observer = PilotObserver()
    observe(observer)
    snap = observer.snapshot()
    assert snap["pilot_sample_count"] == 1
    assert snap["pilot_sample"]["received_at"] == 100.
    snap["pilot_sample"]["sticks"]["yaw"] = 123
    assert observer.snapshot()["pilot_sample"]["sticks"]["yaw"] == -20


def test_radio_active_and_automatic_phase_do_not_invent_valid_output():
    sample = parse_pilot_sample(LINE.replace(b"AADAA11", b"AADMA00").replace(b"-205 51", b"0 0"))
    assert sample["state"] == "A" and sample["pitch_phase"] == "A"
    assert not sample["lua_outputs"]["yaw"]["valid"]
    assert not sample["lua_outputs"]["pitch"]["valid"]


@pytest.mark.parametrize("line", [
    LINE + b" extra", LINE.replace(b"AP1", b"AP2"), LINE.replace(b" 2 ", b" 02 "),
    LINE.replace(b" 2 ", b" 2147483648 "), LINE.replace(b" 23 ", b" 1.5 "),
    LINE.replace(b" 23 ", b" -0 "), LINE.replace(b"-1024", b"-1025"),
    LINE.replace(b"-205", b"-206"), LINE.replace(b" 51", b" 52"),
    LINE.replace(b"AADAA11", b"MADAA11"), LINE.replace(b"AADAA11", b"AANAA11"),
    LINE.replace(b"AADAA11", b"AADMA11"), LINE.replace(b"AADAA11", b"AAYAN11"),
    LINE.replace(b"AADAA11", b"AADAA00"), b"AP1 " + b"x" * 93,
])
def test_invalid_sample_is_refused(line):
    with pytest.raises(ProbeError):
        parse_pilot_sample(line)


def test_duplicate_and_old_ticket_sample_do_not_refresh_receipt_or_count():
    observer = PilotObserver()
    observe(observer)
    observe(observer, at=101.)
    observe(observer, status=replace(STATUS, ticket=4), at=102.)
    assert observer.snapshot()["pilot_sample"]["received_at"] == 100.
    assert observer.count == 1
    observe(observer, LINE.replace(b" 2 3 4 ", b" 2 4 4 "),
            status=replace(STATUS, ticket=4), at=103.)
    assert observer.count == 2
    assert observer.sample["received_at"] == 103.


@pytest.mark.parametrize("status", [None, replace(STATUS, ack=5), replace(STATUS, generation=3),
                                     replace(STATUS, yaw_phase="M")])
def test_sample_must_match_independently_accepted_radio_status(status):
    observer = PilotObserver()
    observe(observer, status=status)
    assert observer.snapshot() == {"pilot_sample": None, "pilot_sample_count": 0}


def test_missing_old_script_data_stays_unknown_and_rotation_discards_current_sample():
    observer = PilotObserver()
    assert observer.snapshot()["pilot_sample"] is None
    observe(observer)
    observer.clear()
    observe(observer, LINE.replace(b"1234abcd", b"abcdef01"))
    assert observer.sample is None and observer.count == 1
    observer.observe(LINE, status=STATUS, session="1234abcd", pending_begin=True, received_at=101.)
    assert observer.sample is None


def test_only_pilot_framing_gets_larger_bound_and_fragmented_maximum_counters_work():
    maximum = (b"AP1 ffffffff 2147483647 2147483647 2147483647 AADAA11 "
               b"-1024 -1024 -1024 -1024 -205 -51")
    assert 64 < len(maximum) < 96
    parser = PilotLines()
    assert parser.feed(maximum[:40]) == []
    assert parser.feed(maximum[40:] + b"\nAY1 abcdef12 1 2 0 M M\n") == [maximum, b"AY1 abcdef12 1 2 0 M M"]
    assert parse_pilot_sample(maximum)["ack"] == 2**31 - 1
    for line in (b"X" * 65, b"AP1 " + b"X" * 93):
        with pytest.raises(ProbeError, match="oversized"):
            PilotLines().feed(line)


class ReadOnlyPort:
    def __init__(self, data):
        self.data = data

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, maximum):
        result, self.data = self.data[:maximum], self.data[maximum:]
        return result

    def write(self, _):
        raise AssertionError("a pilot observation must not cause a serial write")


@pytest.mark.parametrize("stream_class", [YawStream, DistanceStream])
def test_additive_sample_cannot_renew_radio_health_ack_or_trigger_a_send(stream_class):
    port = ReadOnlyPort(b"AP1 1234abcd 2 3 0 MMNNN00 1024 -1024 -800 20 0 0\n")
    host = stream_class(port, clock=lambda: 100.)
    host.session, host.greeted = "1234abcd", True
    host.status = (RadioStatus("1234abcd", 2, 3, 0, "M", "M", "N", "N", "N")
                   if stream_class is DistanceStream else YawStatus("1234abcd", 2, 3, 0, "M", "M"))
    host.last_status_at, host.last_ack_at = 99.5, 99.4
    host.step(SourceSample(None, 100., error=True))
    assert host.snapshot()["pilot_sample"]["sticks"]["roll"] == 1024
    assert host.last_status_at == 99.5 and host.last_ack_at == 99.4
    assert host.sent_sequence == 0 and host.status.ack == 0


@pytest.mark.parametrize("stream_class", [YawStream, DistanceStream])
def test_malformed_additive_sample_fails_closed_before_any_command(stream_class):
    port = ReadOnlyPort(b"AP1 1234abcd 2 3 0 MMNNN00 1025 0 -800 20 0 0\n")
    host = stream_class(port, clock=lambda: 100.)
    host.session, host.greeted = "1234abcd", True
    with pytest.raises(ProbeError, match="out-of-range AP1"):
        host.step(SourceSample(None, 100., error=True))
    assert host.failed and host.snapshot()["pilot_sample"] is None
