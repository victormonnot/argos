"""Recorded assistance reports preserve uncertainty and archive boundaries."""
from copy import deepcopy
import asyncio
import json
import sqlite3
from threading import get_ident

import pytest

from argos.console.framing_analysis import MAX_INTERVALS, MAX_SERIES, framing_report
from argos.console.visual_recording import VisualArchive, VisualArchiveError, _public
from test_console_visual_recording import (
    BINDING, IDENTIFIER, START, capture, finish, image, path,
)
from test_console_visual_integration import (
    camera_only, complete_camera_recording, start, stop,
)


def control(phase="active", profile="full", response="normal", **framing):
    return {"enabled": True, "owned": True, "phase": "armed", "vehicle": {"armed": True},
            "framing": {"phase": phase, "paused": False, "profile": profile,
                        "range_response": response, "target_id": 4, "reference_height": .2,
                        "height": .24, "error_x": -.1, "error_y": .05, "frame_age_s": .05,
                        **framing}}


def report(values, *, end=12., events=(), event_count=None, visual_end=None):
    rows = [(index, at, state, received) for index, (at, state, received) in enumerate(values)]
    return framing_report(iter(rows), list(events), {
        "started_at": 10., "ended_at": end if visual_end is None else visual_end,
        "session_duration_s": end - 10., "sample_age_limit_s": .35,
        "video_age_limit_s": .5, "events": len(events) if event_count is None else event_count})


def test_exclusive_durations_hold_samples_only_until_age_limit_and_preserve_gaps():
    idle = control("idle")
    inactive = {**idle, "owned": False}
    result = report([(10.2, idle, None), (10.3, control(), 10.25),
                     (10.5, control(paused=True, reason="Waiting for selected target"), 10.4),
                     (10.8, control(profile="pilot_throttle"), 10.75),
                     (11., control("takeover", reason="Target did not recover"), 10.9),
                     (11.1, inactive, None)])
    assert result["durations_s"] == pytest.approx({
        "manual": .1, "full": .2, "pilot_throttle": .2, "unknown_profile": 0.,
        "paused": .3, "takeover": .1, "inactive": .35, "unknown": .75})
    assert sum(result["durations_s"].values()) == pytest.approx(2.)
    assert result["coverage"]["sampled_s"] == pytest.approx(1.25)
    assert result["coverage"]["metric_s"] == pytest.approx(.4)
    assert result["coverage"]["metric_samples"] == 2
    assert result["intervals"][2]["state"] == "full"
    assert result["intervals"][3]["reason"] == "Waiting for selected target"
    assert result["series"][2]["error_x"] is None  # Paused is not active correction.


def test_legacy_choices_remain_unknown_and_no_control_is_not_manual():
    legacy = control()
    del legacy["framing"]["profile"]
    del legacy["framing"]["range_response"]
    result = report([(10., None, None), (10.2, {"phase": "armed"}, None),
                     (10.4, legacy, 10.35)], end=10.6)
    assert result["durations_s"]["manual"] == 0
    assert result["durations_s"]["unknown"] == pytest.approx(.4)
    assert result["durations_s"]["unknown_profile"] == pytest.approx(.2)
    point = result["series"][-1]
    assert point["profile"] is None and point["range_response"] is None
    assert point["size_error"] == pytest.approx(.2)
    assert point["error_x"] == -.1


def test_new_reference_target_profile_or_response_breaks_chart_and_interval():
    values = [control(), control(reference_height=.3), control(target_id=5),
              control(profile="pilot_throttle"), control(response="gentle")]
    result = report([(10. + index * .1, state, 10. + index * .1)
                     for index, state in enumerate(values)], end=10.5)
    assert len(result["intervals"]) == 5
    assert len({point["segment"] for point in result["series"]}) == 5
    assert result["series"][1]["reference_height"] == .3
    assert result["series"][1]["size_error"] == pytest.approx(-.2)
    assert result["series"][-1]["range_response"] == "gentle"


@pytest.mark.parametrize("changes,received", [({"frame_age_s": .6}, 10.), ({}, None),
    ({"height": None}, 10.), ({"reference_height": 0.}, 10.), ({"error_x": float("nan")}, 10.)])
def test_missing_stale_or_invalid_evidence_never_becomes_a_metric(changes, received):
    result = report([(10., control(**changes), received)], end=10.2)
    assert result["durations_s"]["full"] == pytest.approx(.2)
    assert result["coverage"]["metric_s"] == 0
    assert result["series"][0]["valid"] is False
    assert result["series"][0]["error_x"] is None and result["series"][0]["size_error"] is None
    json.dumps(result, allow_nan=False)


def test_metrics_and_state_coverage_end_at_recorded_freshness_and_capture_end():
    result = report([(10.6, control(frame_age_s=.4), 10.2),
                     (11., control(), 11.)], end=12., visual_end=11.1)
    assert result["coverage"]["metric_s"] == pytest.approx(.2)
    assert result["durations_s"]["full"] == pytest.approx(.45)
    assert result["coverage"]["unknown_s"] == pytest.approx(1.55)
    assert result["series"][0]["segment"] != result["series"][1]["segment"]
    assert result["duration_s"] == 2 and result["visual_duration_s"] == pytest.approx(1.1)


def test_interruption_is_recorded_once_and_not_moved_from_before_capture():
    before = {**control(), "interruption": {"at": 9., "reason": "An older session"}}
    interrupted = {**control(), "interruption": {"at": 10.15, "reason": "Control input expired"}}
    future = {**control(), "interruption": {"at": 12., "reason": "Invalid future evidence"}}
    result = report([(10., before, 10.), (10.2, interrupted, 10.2),
                     (10.3, interrupted, 10.3), (10.4, future, 10.4)], end=10.5)
    assert result["interruptions_count"] == 1
    event, = result["events"]
    assert event["at_s"] == pytest.approx(.15)
    assert event["detail"] == "Control input expired" and event["kind"] == "interruption"


def test_large_report_bounds_presentation_without_changing_total_duration_or_extrema():
    values = []
    for index in range(2500):
        state = control(response="normal" if index % 2 else "gentle",
                        error_x=.9 if index == 1432 else -.8 if index == 1433 else .1)
        values.append((10. + index * .1, state, 10. + index * .1))
    result = report(values, end=260.)
    assert len(result["series"]) <= MAX_SERIES and result["series_downsampled"]
    assert max(point["error_x"] for point in result["series"]) == .9
    assert min(point["error_x"] for point in result["series"]) == -.8
    assert len(result["intervals"]) == MAX_INTERVALS and result["intervals_truncated"]
    assert result["intervals_count"] == 2500
    assert result["durations_s"]["full"] == pytest.approx(250.)
    assert result["coverage"]["samples"] == 2500


def test_no_samples_retains_the_entire_unknown_window():
    result = report([])
    assert result["durations_s"]["unknown"] == 2
    assert result["coverage"]["sampled_s"] == 0
    assert result["series"] == []


def test_range_response_allowlist_records_only_known_nested_enum():
    assert _public({"range_response": "normal", "framing": {"range_response": "gentle"}}, top=True) == {
        "framing": {"range_response": "gentle"}}
    for value in ({"token": "secret"}, ["normal"], "unknown", None):
        assert _public({"framing": {"range_response": value}}, top=True) == {"framing": {}}


def test_archive_report_uses_same_samples_as_replay_without_loading_images(tmp_path, monkeypatch):
    writer = capture(tmp_path)
    assert writer.append(START, frame=image(), control=control())
    assert writer.append(START + .2, frame=image(2, START + .2), control=control(response="responsive"))
    finish(writer, START + .4)
    archive = VisualArchive(tmp_path)
    metadata = archive.metadata(IDENTIFIER, **BINDING)
    # The archive validates metadata; reports do not decode archived JPEGs.
    monkeypatch.setattr("argos.console.visual_recording._check_jpeg", lambda *args: pytest.fail("report decoded JPEG"))
    result = archive.framing_report(IDENTIFIER, revision=metadata["revision"], duration_s=1., **BINDING)
    assert result["coverage"]["samples"] == 2
    assert result["durations_s"]["full"] == pytest.approx(.4)
    assert result["durations_s"]["unknown"] == pytest.approx(.6)
    for point in result["series"]:
        view = archive.replay(IDENTIFIER, point["at_s"], revision=metadata["revision"], **BINDING)
        assert point["range_response"] == view["sample"]["control"]["framing"]["range_response"]
    assert any("range response responsive" in event["detail"] for event in result["events"])
    with pytest.raises(VisualArchiveError, match="ends after"):
        archive.framing_report(IDENTIFIER, revision=metadata["revision"], duration_s=.2, **BINDING)


def test_archive_rejects_changed_file_even_after_cached_validation(tmp_path):
    writer = capture(tmp_path)
    writer.append(START, control=control())
    finish(writer)
    archive = VisualArchive(tmp_path)
    metadata = archive.metadata(IDENTIFIER, **BINDING)
    with sqlite3.connect(path(tmp_path)) as db:
        db.execute("UPDATE samples SET control=?", (json.dumps(control(response="gentle")),))
    with pytest.raises(VisualArchiveError, match="changed"):
        archive.framing_report(IDENTIFIER, revision=metadata["revision"], **BINDING)


def test_http_report_is_read_only_runs_archive_work_in_thread_and_matches_revision(camera_only, monkeypatch):
    f = camera_only
    metadata, _ = complete_camera_recording(f)
    original = VisualArchive.framing_report
    workers = []
    def checked(self, *args, **kwargs):
        workers.append(get_ident())
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(VisualArchive, "framing_report", checked)
    before = deepcopy(f.session.control.state(f.now[0]))
    response = f.client.get(f"/api/recordings/{metadata['id']}/framing-report", params={
        "revision": metadata["revision"], "visual_revision": metadata["visual"]["revision"]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["revision"] == metadata["revision"]
    assert result["visual_revision"] == metadata["visual"]["revision"]
    assert result["coverage"]["metric_samples"] == 0
    assert result["durations_s"]["manual"] == 0
    assert workers and workers[0] != get_ident()
    assert f.session.control.state(f.now[0]) == before
    assert f.session.link is None and f.session.camera is None


@pytest.mark.parametrize("wrong_key", ["revision", "visual_revision"])
def test_http_report_fences_both_revisions(camera_only, wrong_key):
    f = camera_only
    metadata, _ = complete_camera_recording(f)
    params = {"revision": metadata["revision"], "visual_revision": metadata["visual"]["revision"]}
    params[wrong_key] = "0" * 64
    response = f.client.get(f"/api/recordings/{metadata['id']}/framing-report", params=params)
    assert response.status_code == 409


def test_http_report_rejects_an_active_capture(camera_only):
    f = camera_only
    identifier = start(f)
    response = f.client.get(f"/api/recordings/{identifier}/framing-report", params={
        "revision": "0" * 64, "visual_revision": "0" * 64})
    assert response.status_code == 404
    stop(f, identifier)
