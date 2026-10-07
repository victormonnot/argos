"""Boundary regressions for offline comparisons, using synthetic measurements."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from argos.perception.image_tracks import ImageTracker
from argos.perception.tracker_comparison import make_tracker, tracker_metadata


@pytest.fixture
def pixels():
    return np.random.default_rng(31).integers(0, 256, (240, 320, 3), dtype=np.uint8)


@pytest.fixture(params=["bytetrack", "botsort"])
def native_name(request):
    for module in ("scipy", "lap", "cython_bbox", "cv2"):
        pytest.importorskip(module)
    return request.param


def box(x=.2, score=.9):
    return {"box": [x, .1, .15, .7], "confidence": score}


def update(tracker, detections, at, pixels):
    return tracker.update(detections, at, image=pixels, width=320, height=240)


def test_image_adapter_is_existing_tracker_without_native_dependency_imports():
    adapter, baseline = make_tracker("image"), ImageTracker()
    for stamp, rows in enumerate([[box()], [box(.21)], [], [box(.22, .4)], [box(.23)]]):
        assert adapter.update(rows, stamp * .1) == baseline.update(rows, stamp * .1)
        assert adapter.last_detection_indices == list(range(len(rows)))
        assert adapter.last_unassigned == []
    result = subprocess.run([sys.executable, "-c",
        "import sys; from argos.perception.tracker_comparison import make_tracker; "
        "make_tracker('image'); assert 'scipy' not in sys.modules; assert 'torch' not in sys.modules"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_vendor_files_match_pinned_provenance():
    vendor = Path(__file__).resolve().parents[1] / "argos/perception/_vendor"
    provenance = json.loads((vendor / "provenance.json").read_text())
    for package, record in provenance.items():
        assert record["license"] == "MIT"
        assert len(record["commit"]) == 40
        assert (vendor / package / "LICENSE").read_text().startswith("MIT License")
        for name, evidence in record["files"].items():
            assert hashlib.sha256((vendor / package / name).read_bytes()).hexdigest() == evidence["vendored_sha256"]


def test_low_scores_continue_existing_identity_but_never_create_one(native_name, pixels):
    tracker = make_tracker(native_name)
    first = update(tracker, [box()], 0, pixels)[0]
    low = box(.205, .35)
    observed = update(tracker, [low], .1, pixels)
    assert observed == [{**low, "track_id": first["track_id"]}]
    fresh = make_tracker(native_name)
    assert update(fresh, [box(score=.35)], 0, pixels) == []
    assert fresh.last_unassigned[0]["detection_index"] == 0


def test_missing_detection_never_returns_prediction_but_short_gap_can_reactivate(native_name, pixels):
    tracker = make_tracker(native_name)
    identity = update(tracker, [box()], 0, pixels)[0]["track_id"]
    assert update(tracker, [], .1, pixels) == []
    assert tracker.last_detection_indices == []
    assert update(tracker, [box(.21)], .2, pixels)[0]["track_id"] == identity


def test_original_measurement_and_detection_index_survive_native_reordering(native_name, pixels):
    tracker = make_tracker(native_name)
    update(tracker, [box(.2), box(.65, .8)], 0, pixels)
    rows = [box(.66, .85), box(.22, .95)]
    saved = deepcopy(rows)
    outputs = update(tracker, rows, .1, pixels)
    assert rows == saved
    assert tracker.last_detection_indices == [1, 0]
    for output, index in zip(outputs, tracker.last_detection_indices):
        assert output["box"] == rows[index]["box"]
        assert output["confidence"] == rows[index]["confidence"]
    # Kalman geometry is different; the consumer must receive actual measurements.
    smoothed = tracker._native.tracked_stracks[0].tlwh / np.array([320, 240, 320, 240])
    assert not np.array_equal(smoothed, outputs[0]["box"])


def test_native_strict_high_boundary_and_birth_filter_stay_visible(native_name, pixels):
    tracker = make_tracker(native_name)
    rows = [box(.1, .5), box(.65, .55)]
    assert update(tracker, rows, 0, pixels) == []
    assert [row["reason"] for row in tracker.last_unassigned] == [
        "native_strict_high_boundary", "native_unmatched_below_birth_threshold"]
    assert [row["detection_index"] for row in tracker.last_unassigned] == [0, 1]


def test_native_confirmation_difference_is_explicit(native_name, pixels):
    tracker = make_tracker(native_name)
    update(tracker, [], 0, pixels)
    born = update(tracker, [box()], .1, pixels)
    if native_name == "bytetrack":
        assert born == []
        assert tracker.last_unassigned[0]["reason"] == "native_unconfirmed"
    else:
        assert len(born) == 1
        assert tracker.last_output_confirmed == [False]
    confirmed = update(tracker, [box(.205)], .2, pixels)
    assert confirmed[0]["track_id"] == 1
    assert tracker.last_output_confirmed == [True]


def test_instances_do_not_steal_ids_from_each_other_and_reset_accepts_new_clock(native_name, pixels):
    first, second = make_tracker(native_name), make_tracker(native_name)
    assert update(first, [box()], 5, pixels)[0]["track_id"] == 1
    assert update(second, [box()], 0, pixels)[0]["track_id"] == 1
    update(first, [box(), box(.7)], 5.1, pixels)
    outputs = update(first, [box(), box(.7)], 5.2, pixels)
    assert {row["track_id"] for row in outputs} == {1, 2}
    first.reset()
    assert update(first, [box()], 0, pixels)[0]["track_id"] == 1


def test_native_memory_is_per_analyzed_update_not_wall_clock(native_name, pixels):
    tracker = make_tracker(native_name)
    identity = update(tracker, [box()], 0, pixels)[0]["track_id"]
    assert update(tracker, [box()], 5, pixels)[0]["track_id"] == identity
    for step in range(1, 11):
        assert update(tracker, [], 5 + step * .1, pixels) == []
    update(tracker, [box()], 6.1, pixels)
    assert update(tracker, [box()], 6.2, pixels)[0]["track_id"] != identity
    assert tracker_metadata(native_name)["buffer_updates"] == 7


def test_diagnostic_callback_failure_cannot_change_association(native_name, pixels):
    def broken(_):
        raise RuntimeError("observer unavailable")
    plain, traced = make_tracker(native_name), make_tracker(native_name, on_diagnostic=broken)
    for step, rows in enumerate([[box()], [box(.205, .4)], [], [box(.21)]]):
        assert update(plain, rows, step * .1, pixels) == update(traced, rows, step * .1, pixels)


@pytest.mark.parametrize("stamp", [-1, float("nan"), float("inf"), True, 0])
def test_invalid_timestamps_fail_before_advancing_native_state(native_name, pixels, stamp):
    tracker = make_tracker(native_name)
    update(tracker, [box()], 0, pixels)
    with pytest.raises(ValueError):
        update(tracker, [box()], stamp, pixels)
    assert tracker._native.frame_id == 1


def test_native_requires_declared_original_dimensions_and_frozen_floor(native_name, pixels):
    tracker = make_tracker(native_name)
    with pytest.raises(ValueError, match="dimensions"):
        tracker.update([box()], 0)
    with pytest.raises(ValueError, match="floor"):
        update(tracker, [box(score=.2)], 0, pixels)
    update(tracker, [box()], 0, pixels)
    with pytest.raises(ValueError, match="dimensions changed"):
        tracker.update([box()], .1, width=640, height=480, image=pixels)
    assert tracker._native.frame_id == 1


def test_bot_requires_pixels_and_propagates_gmc_failure(native_name, pixels, monkeypatch):
    if native_name != "botsort":
        pytest.skip("BoT-SORT-only pixel/GMC contract")
    tracker = make_tracker(native_name)
    with pytest.raises(ValueError, match="original uint8 BGR"):
        tracker.update([box()], 0, width=320, height=240)
    def broken(*_):
        raise RuntimeError("gmc failed")
    monkeypatch.setattr(tracker._native.gmc, "apply", broken)
    with pytest.raises(RuntimeError, match="gmc failed"):
        update(tracker, [box()], 0, pixels)


def test_unknown_tracker_is_rejected():
    with pytest.raises(ValueError, match="unknown"):
        make_tracker("approximate-bot")
