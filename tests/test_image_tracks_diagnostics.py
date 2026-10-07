"""Offline decision evidence must not change image association behavior."""
import json

import pytest

from argos.perception.image_tracks import ImageTracker


def person(x=.1, *, width=.2, confidence=.8):
    return {"box": [x, .2, width, .5], "confidence": confidence}


def descriptor(score=1.):
    return [score, (1 - score ** 2) ** .5] + [0.] * 206


def state(tracker):
    return (tracker._tracks, tracker._last_at, tracker._last_singleton,
            tracker._next_id, tracker._appearance_mode)


@pytest.mark.parametrize("frames", [
    # Empty images and expiration do not renew either kind of evidence.
    [(1., [person()], [descriptor()]), (1.2, [], []),
     (1.3, [person(confidence=.4)], [None]), (2.01, [person()], [descriptor()])],
    # A false appearance contradiction can seed a second overlapping history.
    [(1., [person()], [descriptor()]), (1.1, [person(.11)], [descriptor(0.)]),
     (1.2, [person(.12)], [descriptor(0.)]), (1.36, [person(.12)], [descriptor(0.)])],
    # Ambiguity retires predecessors and later frames keep the replacement IDs.
    [(1., [person()], None), (1.1, [person(.11), person(.12)], None),
     (1.2, [person(.11)], None), (1.3, [person(.11)], None)],
    # Bounded appearance extension retains a disjoint but compatible detection.
    [(1., [person(.3, width=.04)], [descriptor()]),
     (1.2, [person(.4, width=.04)], [descriptor(.98)]), (1.3, [], []),
     (1.4, [person(.5, width=.04)], [descriptor(.98)])],
    # Fresh weak display boxes never become persistent predecessors.
    [(1., [person(confidence=.4)], None), (1.1, [person(confidence=.4)], None),
     (1.2, [person()], None), (1.3, [person(confidence=.4)], None)],
], ids=["gaps-and-expiry", "contradiction-and-retirement", "ambiguity",
        "appearance-extension-and-gap", "weak-display-boxes"])
def test_diagnostics_preserve_outputs_and_every_piece_of_association_state(frames):
    records = []
    plain, observed = ImageTracker(), ImageTracker(on_diagnostic=records.append)
    for index, (at, detections, appearances) in enumerate(frames):
        expected = plain.update(detections, at, appearances=appearances)
        assert observed.update(detections, at, appearances=appearances) == expected
        assert state(observed) == state(plain)
        assert len(records) == index + 1
        assert records[-1]["captured_at"] == at
        # Detached scalar/box records are directly usable in an offline JSONL.
        json.dumps(records[-1], allow_nan=False)
    plain.reset()
    observed.reset()
    assert state(observed) == state(plain)
    assert observed.update([person()], 0) == plain.update([person()], 0)
    assert records[-1]["previous_tracks"] == []
    assert records[-1]["previous_at"] is None


def test_contradiction_diagnostics_distinguish_good_geometry_from_rejected_appearance():
    records = []
    tracker = ImageTracker(on_diagnostic=records.append)
    old = tracker.update([person()], 1., appearances=[descriptor()])[0]["track_id"]
    new = tracker.update([person(.11)], 1.1, appearances=[descriptor(.7)])[0]["track_id"]
    assert new != old
    geometry, extension = records[-1]["edges"]
    assert geometry["stage"] == "strong_geometry"
    assert geometry["track_id"] == old and geometry["detection_index"] == 0
    assert geometry["iou"] > .9 and geometry["similarity"] == pytest.approx(.7)
    assert geometry["reason"] == "appearance_contradiction"
    assert extension["reason"] == "appearance_similarity"
    assert not geometry["accepted"] and not extension["accepted"]
    assert records[-1]["associations"][0]["reason"] == "new_strong"
    assert records[-1]["associations"][0]["previous_track_id"] is None


def test_ambiguity_and_weak_births_have_different_evidence():
    records = []
    tracker = ImageTracker(on_diagnostic=records.append)
    old = tracker.update([person()], 1.)[0]["track_id"]
    tracker.update([person(.11), person(.12), person(.7, confidence=.4)], 1.1)
    decision = records[-1]
    assert decision["retired_tracks"] == [{"track_id": old, "reason": "strong_ambiguity"}]
    assert all(edge["reason"] == "ambiguous_geometry" for edge in decision["edges"])
    assert [item["blocked_by_strong_ambiguity"] for item in decision["associations"]] == [True, True, False]
    weak = decision["associations"][2]
    assert weak["reason"] == "weak_display_only" and not weak["remembered"]
    assert weak["track_id"] not in decision["remembered_track_ids"]


def test_expansion_diagnostics_explain_success_then_failure_to_bridge_an_empty_image():
    records = []
    tracker = ImageTracker(on_diagnostic=records.append)
    old = tracker.update([person(.3, width=.04)], 1., appearances=[descriptor()])[0]["track_id"]
    tracker.update([person(.4, width=.04)], 1.1, appearances=[descriptor()])
    assert records[-1]["associations"][0]["reason"] == "appearance_extension"
    assert records[-1]["associations"][0]["track_id"] == old
    assert records[-1]["edges"][0]["reason"] == "geometry_gate"
    assert records[-1]["edges"][1]["accepted"]
    tracker.update([], 1.2, appearances=[])
    assert records[-1]["associations"] == [] and records[-1]["edges"] == []
    tracker.update([person(.5, width=.04)], 1.3, appearances=[descriptor()])
    assert records[-1]["edges"][1]["reason"] == "not_previous_frame"
    assert records[-1]["associations"][0]["track_id"] != old


def test_diagnostics_explain_margin_against_another_current_candidate():
    records = []
    tracker = ImageTracker(on_diagnostic=records.append)
    tracker.update([person(.3, width=.04)], 1., appearances=[descriptor()])
    tracker.update([person(.39, width=.04), person(.41, width=.04)], 1.1,
                   appearances=[descriptor(.99), descriptor(.94)])
    extensions = [edge for edge in records[-1]["edges"] if edge["stage"] == "appearance_extension"]
    assert extensions[0]["reason"] == "appearance_margin"
    assert extensions[0]["margin"] == pytest.approx(.05)
    assert extensions[1]["reason"] == "appearance_similarity"


def test_expiration_and_obsolete_singleton_retirement_are_not_conflated():
    records = []
    tracker = ImageTracker(on_diagnostic=records.append)
    old = tracker.update([person()], 1., appearances=[descriptor()])[0]["track_id"]
    replacement = tracker.update([person(.11)], 1.1, appearances=[descriptor(0.)])[0]["track_id"]
    tracker.update([person(.12)], 1.2, appearances=[descriptor(0.)])
    tracker.update([person(.12)], 1.36, appearances=[descriptor(0.)])
    assert records[-1]["retired_tracks"] == [{"track_id": old, "reason": "obsolete_singleton"}]
    assert records[-1]["expired_track_ids"] == []
    tracker.update([], 2.1, appearances=[])
    assert records[-1]["expired_track_ids"] == [replacement]
    assert records[-1]["retired_tracks"] == []
    assert records[-1]["remembered_track_ids"] == []


def test_callback_mutation_and_failure_cannot_change_output_or_tracker_state():
    def broken_callback(record):
        for item in record["detections"] + record["previous_tracks"]:
            item["box"][0] = .7
            item["confidence"] = 0.
        record["associations"].clear()
        record["remembered_track_ids"].clear()
        raise RuntimeError("offline diagnostic destination failed")

    observed = ImageTracker(on_diagnostic=broken_callback)
    plain = ImageTracker()
    for at in (1., 1.1, 1.2):
        assert observed.update([person()], at) == plain.update([person()], at)
        assert state(observed) == state(plain)


def test_invalid_updates_do_not_emit_records_or_change_state():
    records = []
    tracker = ImageTracker(on_diagnostic=records.append)
    tracker.update([person()], 1.)
    before = state(tracker)
    for detections, at in (([person()], 1.), ([person(confidence=2.)], 1.1)):
        with pytest.raises(ValueError):
            tracker.update(detections, at)
        assert len(records) == 1 and state(tracker) == before
    with pytest.raises(ValueError, match="callable"):
        ImageTracker(on_diagnostic=1)


def test_default_path_builds_no_diagnostics(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("default tracking built diagnostic evidence")

    monkeypatch.setattr(ImageTracker, "_diagnostic", forbidden)
    monkeypatch.setattr(ImageTracker, "_pair_diagnostic", forbidden)
    tracker = ImageTracker()
    first = tracker.update([person()], 1., appearances=[descriptor()])
    assert tracker.update([person()], 1.1, appearances=[descriptor()]) == first
