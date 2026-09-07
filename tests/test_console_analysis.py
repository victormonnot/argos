"""Timing summaries retain silence and never manufacture a receipt/sensor age."""
import json

import pytest

from argos.backends.mavlink import Received, Recording
from argos.console.analysis import analyze_recording


def recording(times=(), *, start=0., end=10.):
    events = tuple(Received(at, 1, 1, index % 256, 30, "ATTITUDE", {}, b"")
                   for index, at in enumerate(times))
    return Recording(start, end, "unused", events)


def test_exact_boundaries_final_receipt_and_peak_before_reset():
    result = analyze_recording(recording([10., 12., 13., 16.], start=10., end=16.), bins=3)
    assert [item["events"] for item in result["bins"]] == [1, 2, 1]
    assert [item["rate_hz"] for item in result["bins"]] == [.5, 1., .5]
    assert [item["max_age_s"] for item in result["bins"]] == [2., 1., 3.]
    assert result["summary"] == {"mean_rate_hz": 4 / 6, "max_gap_s": 3.}
    assert result["gaps"] == [
        {"start_s": 3., "end_s": 6., "duration_s": 3., "boundary": "interior"},
        {"start_s": 0., "end_s": 2., "duration_s": 2., "boundary": "interior"},
        {"start_s": 2., "end_s": 3., "duration_s": 1., "boundary": "interior"},
    ]


def test_age_before_first_receipt_is_unknown_then_accounts_for_the_tail():
    result = analyze_recording(recording([4.], end=10.), bins=5)
    assert [item["events"] for item in result["bins"]] == [0, 0, 1, 0, 0]
    assert [item["max_age_s"] for item in result["bins"]] == [None, None, 2., 4., 6.]
    assert result["gaps"] == [
        {"start_s": 4., "end_s": 10., "duration_s": 6., "boundary": "end"},
        {"start_s": 0., "end_s": 4., "duration_s": 4., "boundary": "start"},
    ]


def test_first_receipt_inside_bin_does_not_use_capture_start_as_previous_receipt():
    result = analyze_recording(recording([1.5], end=2.), bins=1)
    assert result["bins"][0]["max_age_s"] == .5
    assert result["summary"]["max_gap_s"] == 1.5


def test_same_timestamp_receipts_have_no_zero_duration_gap_or_invented_time():
    result = analyze_recording(recording([0., 1., 1., 1., 2.], end=3.), bins=3)
    assert [item["events"] for item in result["bins"]] == [1, 3, 1]
    assert [item["max_age_s"] for item in result["bins"]] == [1., 1., 1.]
    assert result["gap_count"] == 3
    assert all(item["duration_s"] == 1. for item in result["gaps"])


def test_empty_recording_reports_whole_silence_and_unknown_age():
    result = analyze_recording(recording(end=2.), bins=4)
    assert result["events"] == 0
    assert result["sources"] == result["message_types"] == []
    assert result["summary"] == {"mean_rate_hz": 0., "max_gap_s": 2.}
    assert result["gaps"] == [{"start_s": 0., "end_s": 2., "duration_s": 2., "boundary": "whole"}]
    assert all(item["events"] == 0 and item["rate_hz"] == 0. and item["max_age_s"] is None
               for item in result["bins"])


@pytest.mark.parametrize("events", [(), (5.,), (5., 5., 5.)])
def test_zero_duration_has_one_bin_and_no_division_by_zero(events):
    result = analyze_recording(recording(events, start=5., end=5.), bins=400)
    assert result["bins"] == [{"start_s": 0., "end_s": 0., "events": len(events),
                               "rate_hz": None, "max_age_s": 0. if events else None}]
    assert result["summary"] == {"mean_rate_hz": None, "max_gap_s": 0.}
    assert result["gaps"] == [] and result["gap_count"] == 0
    json.dumps(result, allow_nan=False)


def test_filters_restrict_timing_but_keep_global_source_and_type_counts():
    events = tuple(Received(at, system, component, index, kind, "unused", {}, b"")
                   for index, (at, system, component, kind) in enumerate([
                       (0., 1, 1, 0), (1., 0, 0, 30), (2., 1, 1, 30),
                       (3., 1, 42, 30), (4., 1, 1, 30), (5., 255, 255, 0xffffff)]))
    log = Recording(0., 6., "unused", events)
    all_data = analyze_recording(log)
    selected = analyze_recording(log, system=1, component=1, message_id=30, bins=3)
    assert selected["filter"] == {"system": 1, "component": 1, "message_id": 30}
    assert selected["events"] == 2
    assert [item["events"] for item in selected["bins"]] == [0, 1, 1]
    assert [item["max_age_s"] for item in selected["bins"]] == [None, 2., 2.]
    assert selected["sources"] == all_data["sources"]
    assert selected["message_types"] == all_data["message_types"]
    assert selected["sources"] == [{"system": 1, "component": 1, "events": 3},
                                   {"system": 0, "component": 0, "events": 1},
                                   {"system": 1, "component": 42, "events": 1},
                                   {"system": 255, "component": 255, "events": 1}]
    assert selected["message_types"] == [{"message_id": 0, "type_name": "unused", "events": 1},
                                         {"message_id": 30, "type_name": "unused", "events": 4},
                                         {"message_id": 0xffffff, "type_name": "unused", "events": 1}]
    assert analyze_recording(log, message_id=30)["events"] == 4
    assert analyze_recording(log, system=0, component=0)["events"] == 1
    assert analyze_recording(log, message_id=0xffffff)["events"] == 1
    absent = analyze_recording(log, system=9, component=9)
    assert absent["events"] == 0 and absent["gaps"][0]["boundary"] == "whole"
    assert all(item["max_age_s"] is None for item in absent["bins"])


def test_gap_threshold_is_inclusive_but_does_not_change_summary():
    log = recording([0., 1., 3.], end=3.5)
    result = analyze_recording(log, gap_threshold_s=2.)
    assert result["gap_count"] == 1 and result["gaps"][0]["duration_s"] == 2.
    hidden = analyze_recording(log, gap_threshold_s=86400)
    assert hidden["gaps"] == [] and hidden["gap_count"] == 0
    assert hidden["summary"] == result["summary"]


def test_top_gaps_are_bounded_longest_first_with_deterministic_ties():
    times = [0.]
    for length in range(1, 151):
        times.append(times[-1] + length)
    result = analyze_recording(recording(times, end=times[-1]), gap_threshold_s=1.)
    assert result["gap_count"] == 150 and result["gaps_truncated"] is True
    assert len(result["gaps"]) == 100
    assert [item["duration_s"] for item in result["gaps"]] == list(range(150, 50, -1))
    tied = analyze_recording(recording(range(151), end=150.))
    assert [item["start_s"] for item in tied["gaps"]] == list(range(100))
    assert tied["gap_count"] == 150 and tied["gaps_truncated"] is True


def test_requested_bin_bounds_and_complete_count_for_dense_receipts():
    log = recording([index / 100 for index in range(1001)], end=10.)
    for bins in (1, 160, 400):
        result = analyze_recording(log, bins=bins)
        assert len(result["bins"]) == bins
        assert sum(item["events"] for item in result["bins"]) == result["events"] == 1001
        assert result["bins"][0]["start_s"] == 0.
        assert result["bins"][-1]["end_s"] == 10.
        assert sum(item["rate_hz"] * (item["end_s"] - item["start_s"]) for item in result["bins"]) == pytest.approx(1001)
        json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("kwargs", [
    {"system": 1}, {"component": 1}, {"system": True, "component": 1},
    {"system": 1., "component": 1}, {"system": -1, "component": 0},
    {"system": 1, "component": 256}, {"message_id": True},
    {"message_id": -1}, {"message_id": 0x1000000}, {"message_id": "30"},
    {"bins": True}, {"bins": 0}, {"bins": 401}, {"bins": 1.5},
    {"gap_threshold_s": 0}, {"gap_threshold_s": -1}, {"gap_threshold_s": True},
    {"gap_threshold_s": float("inf")}, {"gap_threshold_s": float("nan")},
    {"gap_threshold_s": 86401}, {"gap_threshold_s": "1"}, {"gap_threshold_s": 10**400},
])
def test_invalid_filters_are_refused(kwargs):
    with pytest.raises(ValueError):
        analyze_recording(recording(), **kwargs)


@pytest.mark.parametrize("log", [recording(start=-1), recording(start=2, end=1),
                                  recording(end=float("inf")), recording([3., 2.]),
                                  recording([-1.]), recording([11.]),
                                  recording([float("nan")])])
def test_invalid_recording_times_are_refused(log):
    with pytest.raises(ValueError):
        analyze_recording(log)


def test_extreme_duration_does_not_emit_nonfinite_rates_or_zero_width_bins():
    with pytest.raises(ValueError):
        analyze_recording(recording(end=5e-324), bins=400)
    with pytest.raises(ValueError):
        analyze_recording(recording([0.], end=5e-324), bins=1)
