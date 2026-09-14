"""Observation metrics stay faithful to samples, without inferred identities."""
from copy import deepcopy
import json
import math

import pytest

from argos.perception.evaluation_report import render_markdown, summarize


def detection(identifier=1, confidence=.8):
    return {"box": [.2, .1, .25, .5], "confidence": confidence, "track_id": identifier}


def row(index, at=None, *, segment=0, video_id="camera", tiny=(), small=(), inference=10, processing=12,
        excluded_before=False):
    received = index * .1 if at is None else at
    return {"index": index, "at_s": received, "available_at_s": max(0., received) + .025,
            "segment": segment, "video_id": video_id, "sequence": index,
            "jpeg_sha256": "a" * 64, "excluded_before": excluded_before,
            "models": {"tiny": {"detections": list(tiny), "inference_ms": inference, "processing_ms": processing},
                       "s": {"detections": list(small), "inference_ms": inference * 2,
                             "processing_ms": processing * 2}}}


def envelope(rows):
    return {"format": "argos.vision-comparison", "version": 1, "state": "complete",
            "source": {"id": "recording-id", "environment": "simulation", "duration_s": 8.,
                       "archive_frame_rows": 100, "selected_frames": len(rows),
                       "skipped_counts": {"duplicate_image": 3}, "truncated": True},
            "configuration": {"threads": 4, "warmup_frames": 2},
            "models": {"tiny": {"label": "YOLOX-Tiny", "input_size": 416, "sha256": "a" * 64},
                       "s": {"label": "YOLOX-S", "input_size": 640, "sha256": "b" * 64}},
            "summary": summarize(rows), "limits": ["Recorded image sampling excludes some live observations."]}


def test_empty_comparison_has_counts_and_unavailable_timings_not_zero_latency():
    summary = summarize([])
    assert summary["frame_count"] == 0
    assert summary["pairwise"]["count_disagreement_frames"] == 0
    for model in summary["variants"].values():
        assert model["frame_count"] == model["distinct_display_ids"] == 0
        assert model["empty_runs"] == {"count": 0, "max_sample_count": 0, "examples": []}
        for timing in model["timing_ms"].values():
            assert timing == {"samples": 0, "median": None, "p95": None, "max": None}
    json.dumps(summary, allow_nan=False)


def test_detection_frame_counts_include_weak_ids_without_treating_them_as_strong():
    rows = [row(0, tiny=[detection(1, .35)], small=[detection(9, .9)]),
            row(1, tiny=[detection(2, .499), detection(3, .5)]),
            row(2, small=[detection(9, .9)])]
    tiny = summarize(rows)["variants"]["tiny"]
    assert tiny["frame_count"] == 3
    assert tiny["frames_with_detections"] == 2
    assert tiny["frames_without_detections"] == 1
    assert tiny["frames_with_strong_detections"] == 1
    assert tiny["distinct_display_ids"] == 3
    assert tiny["adjacent_single_detection_id_changes"] == 0


def test_display_ids_are_scoped_to_tracker_reset_segments_not_people():
    rows = [row(0, tiny=[detection(1)]), row(1, tiny=[detection(1)]),
            row(2, segment=1, tiny=[detection(1)]),
            row(3, segment=2, video_id="other-camera", tiny=[detection(2)])]
    tiny = summarize(rows)["variants"]["tiny"]
    assert tiny["distinct_display_ids"] == 3
    assert tiny["adjacent_single_detection_id_changes"] == 0


def test_id_changes_require_adjacent_single_detections_in_one_source_segment():
    rows = [row(0, tiny=[detection(1, .36)]), row(1, tiny=[detection(2, .38)]),
            row(2), row(3, tiny=[detection(3)]),
            row(4, tiny=[detection(3), detection(4)]), row(5, tiny=[detection(5)]),
            row(6, segment=1, tiny=[detection(6)]),
            row(7, segment=1, video_id="another", tiny=[detection(7)])]
    tiny = summarize(rows)["variants"]["tiny"]
    assert tiny["adjacent_single_detection_id_changes"] == 1
    assert tiny["id_change_examples"] == [{
        "segment": 0, "video_id": "camera", "previous_index": 0, "index": 1,
        "previous_at_s": 0., "at_s": .1, "previous_track_id": 1, "track_id": 2,
        "previous_available_at_s": .025, "available_at_s": .125,
        "previous_confidence": .36, "confidence": .38,
    }]


def test_adjacent_singleton_change_over_gap_stays_an_observation_with_both_timestamps():
    rows = [row(0, -.2, tiny=[detection(1)]), row(1, 2., tiny=[detection(2)])]
    tiny = summarize(rows)["variants"]["tiny"]
    assert tiny["adjacent_single_detection_id_changes"] == 1
    assert tiny["id_change_examples"][0]["previous_at_s"] == -.2
    assert tiny["id_change_examples"][0]["at_s"] == 2.


def test_empty_runs_break_on_detection_segment_source_and_reception_gap():
    rows = [row(0, -.7), row(1, 0.), row(2, .1, tiny=[detection()]),
            row(3, .2), row(4, 1.), row(5, 1.1), row(6, 1.2, segment=1),
            row(7, 1.3, segment=1, video_id="another")]
    empty = summarize(rows)["variants"]["tiny"]["empty_runs"]
    assert empty["count"] == 5
    assert empty["max_sample_count"] == 2
    assert [run["sample_count"] for run in empty["examples"]] == [2, 1, 2, 1, 1]
    assert empty["examples"][0]["first_at_s"] == -.7
    assert empty["examples"][0]["last_at_s"] == 0.
    assert all("duration" not in key for run in empty["examples"] for key in run)


def test_excluded_distinct_image_breaks_empty_runs_and_id_comparisons():
    rows = [row(0, tiny=[detection(1)]),
            row(1, tiny=[detection(2)], excluded_before=True)]
    summary = summarize(rows)
    assert summary["variants"]["tiny"]["adjacent_single_detection_id_changes"] == 0
    assert summary["variants"]["s"]["empty_runs"]["count"] == 2
    assert summary["variants"]["s"]["empty_runs"]["max_sample_count"] == 1


def test_timing_median_and_nearest_rank_p95_keep_outliers():
    rows = [row(i, inference=i + 1, processing=(i + 1) * 3) for i in range(20)]
    timing = summarize(rows)["variants"]["tiny"]["timing_ms"]
    assert timing["inference"] == {"samples": 20, "median": 10.5, "p95": 19., "max": 20.}
    assert timing["processing"] == {"samples": 20, "median": 31.5, "p95": 57., "max": 60.}
    singleton = summarize([row(0, inference=2.25)])["variants"]["tiny"]["timing_ms"]["inference"]
    assert singleton == {"samples": 1, "median": 2.25, "p95": 2.25, "max": 2.25}


def test_pairwise_counts_do_not_match_ids_across_models():
    rows = [row(0, tiny=[detection(1)], small=[detection(200)]),
            row(1, tiny=[detection(2)], small=[detection(1), detection(2)]),
            row(2, small=[detection(2)])]
    pairwise = summarize(rows)["pairwise"]
    assert pairwise["count_disagreement_frames"] == 2
    assert [(s["index"], s["tiny_count"], s["s_count"]) for s in pairwise["count_disagreement_examples"]] == [
        (1, 1, 2), (2, 0, 1),
    ]


def test_examples_are_bounded_without_truncating_totals_or_mutating_inputs():
    rows = [row(i, tiny=[detection(i + 1)], excluded_before=False) for i in range(12)]
    before = deepcopy(rows)
    summary = summarize(rows)
    assert rows == before
    tiny = summary["variants"]["tiny"]
    assert tiny["adjacent_single_detection_id_changes"] == 11
    assert len(tiny["id_change_examples"]) == 5
    assert summary["pairwise"]["count_disagreement_frames"] == 12
    assert len(summary["pairwise"]["count_disagreement_examples"]) == 5
    segmented = summarize([row(i, segment=i) for i in range(9)])
    empty = segmented["variants"]["tiny"]["empty_runs"]
    assert empty["count"] == 9 and len(empty["examples"]) == 5
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize("timing", [math.nan, math.inf, -1, True, None])
def test_invalid_timings_cannot_become_a_plausible_report(timing):
    sample = row(0)
    sample["models"]["tiny"]["inference_ms"] = timing
    with pytest.raises(ValueError, match="inference_ms"):
        summarize([sample])


def test_nonincreasing_time_rejected_within_segment_but_negative_start_is_valid():
    with pytest.raises(ValueError, match="increase"):
        summarize([row(0, -.1), row(1, -.1)])
    assert summarize([row(0, -.2), row(1, -.1)])["frame_count"] == 2
    assert summarize([row(0, 1.), row(1, -.1, segment=1)])["frame_count"] == 2


def test_markdown_describes_evidence_and_limits_without_accuracy_or_identity_claims():
    report = envelope([row(0, -.1, tiny=[detection(1, .36)]),
                       row(1, .1, tiny=[detection(2, .4)])])
    before = deepcopy(report)
    result = render_markdown(report)
    assert report == before
    for text in ("YOLOX-Tiny", "YOLOX-S", "Frames without detections", "nearest-rank",
                 "confidence 0.360 → 0.400", "Replay cursor (s): 0.125", "[frames.jsonl](frames.jsonl)",
                 "not a verified identity switch", "not a count of people",
                 "do not establish continuous absence", "does not calculate accuracy or recall",
                 "original archived overlays"):
        assert text in result
    assert "Selection truncated: yes" in result
    assert "duplicate_image: 3" in result
    assert "| Inference p95, ms | 10.000 | 20.000 |" in result
    assert "\n## Model provenance\n\n" in result
    assert "not matched between models" in result
    assert result.endswith("\n")


def test_empty_markdown_has_unavailable_timing_and_no_zero_fps_claim():
    result = render_markdown(envelope([]))
    assert "| Inference median, ms | unavailable | unavailable |" in result
    assert "None observed." in result
    assert "FPS" in result and "real-time FPS" in result


def test_metadata_cannot_break_markdown_tables_or_add_raw_html():
    report = envelope([])
    report["models"]["tiny"]["label"] = "Tiny|<img src=x>\nnew row"
    result = render_markdown(report)
    assert "Tiny\\|\\<img src=x\\> new row" in result
    assert "<img src=x>" not in result
