"""Dense review binding, conditional interventions and honest metric denominators."""
from copy import deepcopy

import pytest

from argos.perception.continuity_reference import (
    CONDITIONS, evaluate_replay, make_suppressions, validate_judgments, validate_reference,
)


BOX = [.2, .15, .2, .65]
OTHER_BOX = [.7, .15, .15, .6]
REFERENCE_SHA = "a" * 64
CACHE_SHA = "b" * 64


def data(count=3):
    frames = [dict(sequence=100 + i, received_at=10 + i * .05,
                   sha256=f"{i + 1:064x}", detections=[dict(box=list(BOX), confidence=.9)])
              for i in range(count)]
    reference = dict(format="argos.continuity.dense-reference", version=1,
        window="review-window", review_status="assistant_reviewed", human_validated=False,
        reviewer="assistant-test", start=10., end=10 + max(count - 1, .01) * .05,
        frames=[dict(sequence=f["sequence"], sha256=f["sha256"], received_at=f["received_at"],
                     review_status="assistant_reviewed", other_people=[],
                     target=dict(identity="person-1", visibility="visible", spatial_quality="clear", box=list(BOX)))
                for f in frames])
    window = dict(name="review-window", start=9., end=15.)
    return frames, reference, window


def annotations(frames, classes=None):
    rows = []
    for frame in frames:
        kinds = (classes or {}).get(frame["sequence"], ["target"] * len(frame["detections"]))
        rows.append(dict(sequence=frame["sequence"], frame_sha256=frame["sha256"], detections=[
            dict(detection_index=index, box=deepcopy(detection["box"]), confidence=detection["confidence"],
                 classification=kind, person_id="person-1" if kind == "target" else "person-2" if kind == "other_person" else None,
                 evidence_id=f"manual-review/frame-{frame['sequence']}/candidate-{index}")
            for index, (detection, kind) in enumerate(zip(frame["detections"], kinds))]))
    return dict(format="argos.continuity.detection-review", version=1,
                review_status="assistant_reviewed", reviewer="assistant-test", human_validated=False,
                reference_sha256=REFERENCE_SHA, cache_sha256=CACHE_SHA, frames=rows)


def checked(frames, reference, window, classes=None):
    refs = validate_reference(reference, frames, window)
    review = annotations(frames, classes)
    judgments = validate_judgments(review, frames, refs, reference_sha256=REFERENCE_SHA, cache_sha256=CACHE_SHA)
    return refs, review, judgments


def test_exact_dense_sixty_frame_coverage_without_promoting_review_status():
    frames, reference, window = data(60)
    original = deepcopy(reference)
    result = validate_reference(reference, frames, window)
    assert len(result) == 60
    assert set(result) == {f["sequence"] for f in frames}
    assert reference == original
    result[100]["target"]["box"][0] = 0
    assert reference == original


@pytest.mark.parametrize("damage", ["missing", "duplicate", "foreign", "sha256", "received_at"])
def test_dense_coverage_or_image_binding_cannot_silently_shift(damage):
    frames, reference, window = data(60)
    if damage == "missing":
        reference["frames"].pop()
    elif damage == "duplicate":
        reference["frames"][-1] = deepcopy(reference["frames"][0])
    elif damage == "foreign":
        reference["frames"][0]["sequence"] = 1000
    elif damage == "sha256":
        reference["frames"][0]["sha256"] = "f" * 64
    else:
        reference["frames"][0]["received_at"] += .001
    with pytest.raises(ValueError):
        validate_reference(reference, frames, window)


@pytest.mark.parametrize("key,value", [
    ("review_status", "human_validated"), ("human_validated", True),
    ("human_validated", None), ("reviewer", " "), ("version", True),
    ("window", "another-window"), ("start", 8.), ("end", 16.),
])
def test_reference_requires_explicit_assistant_status_and_declared_window(key, value):
    frames, reference, window = data()
    reference[key] = value
    with pytest.raises(ValueError):
        validate_reference(reference, frames, window)


@pytest.mark.parametrize("damage", ["wrong_target_id", "missing_clear_box", "outside_box", "other_reuses_target", "duplicate_other"])
def test_reference_identity_and_spatial_contract(damage):
    frames, reference, window = data()
    row = reference["frames"][0]
    if damage == "wrong_target_id":
        row["target"]["identity"] = "person-2"
    elif damage == "missing_clear_box":
        row["target"]["box"] = None
    elif damage == "outside_box":
        row["target"]["box"] = [.9, .2, .3, .6]
    elif damage == "other_reuses_target":
        row["other_people"] = [dict(identity="person-1", box=list(OTHER_BOX))]
    else:
        row["other_people"] = [dict(identity="person-2", box=list(OTHER_BOX))] * 2
    with pytest.raises(ValueError):
        validate_reference(reference, frames, window)


def test_other_people_must_explicitly_distinguish_unknown_from_reviewed_empty():
    frames, reference, window = data(1)
    reference["frames"][0].pop("other_people")
    with pytest.raises(ValueError, match="other_people"):
        validate_reference(reference, frames, window)
    reference["frames"][0]["other_people"] = None
    refs = validate_reference(reference, frames, window)
    assert refs[100]["other_people"] is None
    review = annotations(frames, {100: ["unknown"]})
    labels = validate_judgments(review, frames, refs, reference_sha256=REFERENCE_SHA, cache_sha256=CACHE_SHA)
    assert labels[100][0]["classification"] == "unknown"


@pytest.mark.parametrize("damage", ["reference_hash", "cache_hash", "missing_frame", "duplicate_frame", "image_hash", "missing_detection", "box", "confidence", "index", "evidence", "person_id", "unknown_person"])
def test_candidate_judgments_bind_every_exact_detector_measurement(damage):
    frames, reference, window = data()
    refs = validate_reference(reference, frames, window)
    review = annotations(frames)
    row, label = review["frames"][0], review["frames"][0]["detections"][0]
    if damage == "reference_hash":
        review["reference_sha256"] = "c" * 64
    elif damage == "cache_hash":
        review["cache_sha256"] = "c" * 64
    elif damage == "missing_frame":
        review["frames"].pop()
    elif damage == "duplicate_frame":
        review["frames"][-1] = deepcopy(row)
    elif damage == "image_hash":
        row["frame_sha256"] = "c" * 64
    elif damage == "missing_detection":
        row["detections"] = []
    elif damage == "box":
        label["box"][0] += .01
    elif damage == "confidence":
        label["confidence"] = .89
    elif damage == "index":
        label["detection_index"] = True
    elif damage == "evidence":
        label["evidence_id"] = " "
    elif damage == "person_id":
        label["person_id"] = "person-2"
    else:
        label.update(classification="other_person", person_id="unreviewed-person")
    with pytest.raises(ValueError):
        validate_judgments(review, frames, refs, reference_sha256=REFERENCE_SHA, cache_sha256=CACHE_SHA)


@pytest.mark.parametrize("visibility", ["uncertain", "occluded", "offscreen"])
def test_unobservable_identity_cannot_become_a_confirmed_target_candidate(visibility):
    frames, reference, window = data()
    reference["frames"][0]["target"].update(visibility=visibility, box=None, spatial_quality="uncertain")
    refs = validate_reference(reference, frames, window)
    with pytest.raises(ValueError):
        validate_judgments(annotations(frames), frames, refs,
                           reference_sha256=REFERENCE_SHA, cache_sha256=CACHE_SHA)


@pytest.mark.parametrize("field,value", [("review_status", "human_validated"), ("human_validated", True), ("human_validated", None), ("reviewer", " ")])
def test_candidate_review_cannot_claim_or_imply_human_validation(field, value):
    frames, reference, window = data()
    refs = validate_reference(reference, frames, window)
    review = annotations(frames)
    review[field] = value
    with pytest.raises(ValueError):
        validate_judgments(review, frames, refs, reference_sha256=REFERENCE_SHA, cache_sha256=CACHE_SHA)


def test_empty_detection_frames_remain_required_review_rows():
    frames, reference, window = data()
    frames[1]["detections"] = []
    refs, review, judgments = checked(frames, reference, window)
    assert judgments[101] == []
    assert len(judgments) == len(refs) == 3
    review["frames"].pop(1)
    with pytest.raises(ValueError):
        validate_judgments(review, frames, refs, reference_sha256=REFERENCE_SHA, cache_sha256=CACHE_SHA)


@pytest.mark.parametrize("field", ["box", "confidence"])
def test_boolean_cannot_impersonate_numeric_detector_binding(field):
    frames, reference, window = data(1)
    frames[0]["detections"][0].update(box=[0., .1, .2, .5], confidence=1.)
    refs = validate_reference(reference, frames, window)
    review = annotations(frames)
    if field == "box":
        review["frames"][0]["detections"][0]["box"][0] = False
    else:
        review["frames"][0]["detections"][0]["confidence"] = True
    with pytest.raises(ValueError):
        validate_judgments(review, frames, refs, reference_sha256=REFERENCE_SHA, cache_sha256=CACHE_SHA)


def test_reordered_judgments_are_projected_back_to_exact_source_indices():
    frames, reference, window = data(1)
    frames[0]["detections"].append(dict(box=list(OTHER_BOX), confidence=.8))
    reference["frames"][0]["other_people"] = [dict(identity="person-2", box=list(OTHER_BOX))]
    refs = validate_reference(reference, frames, window)
    review = annotations(frames, {100: ["target", "other_person"]})
    review["frames"][0]["detections"].reverse()
    original = deepcopy(review)
    labels = validate_judgments(review, frames, refs, reference_sha256=REFERENCE_SHA, cache_sha256=CACHE_SHA)
    assert [row["detection_index"] for row in labels[100]] == [0, 1]
    assert [row["classification"] for row in labels[100]] == ["target", "other_person"]
    assert review == original
    labels[100][0]["box"][0] = .99
    assert review == original


def oracle_fixture():
    frames, reference, window = data(1)
    frame = frames[0]
    frame["detections"] = [dict(box=list(BOX), confidence=score) for score in (.8, .9, .9, .99, .95, .97)]
    reference["frames"][0]["other_people"] = [dict(identity="person-2", box=list(OTHER_BOX))]
    classes = {100: ["target", "target", "target", "not_person", "other_person", "unknown"]}
    return frames, *checked(frames, reference, window, classes)[::2]


@pytest.mark.parametrize("condition,expected", [
    ("unchanged", []), ("duplicates_only", [0, 2]),
    ("false_positives_only", [3]), ("both", [0, 2, 3]),
])
def test_oracle_separates_duplicate_and_false_positive_effects_and_protects_other_people(condition, expected):
    frames, _, judgments = oracle_fixture()
    original_frames, original_judgments = deepcopy(frames), deepcopy(judgments)
    suppressions = make_suppressions(condition, judgments, frames)
    assert [item["detection_index"] for item in suppressions] == expected
    assert not {4, 5} & {item["detection_index"] for item in suppressions}
    assert frames == original_frames and judgments == original_judgments
    if suppressions:
        suppressions[0]["expected_box"][0] = .99
        assert frames == original_frames and judgments == original_judgments


def test_duplicate_survivor_depends_only_on_confidence_then_original_index():
    frames, _, judgments = oracle_fixture()
    first = make_suppressions("duplicates_only", judgments, frames)
    for item in judgments[100]:
        item["track_id"] = 99 if item["detection_index"] == 0 else 1
    judgments[100] = list(reversed(judgments[100]))
    second = make_suppressions("duplicates_only", judgments, frames)
    assert {row["detection_index"] for row in first} == {row["detection_index"] for row in second} == {0, 2}
    # Even a selected-ID hint cannot rescue the lower-confidence duplicate.
    assert 0 in {row["detection_index"] for row in second}


def test_two_real_people_are_never_collapsed_even_when_their_boxes_overlap():
    frames, reference, window = data(1)
    frames[0]["detections"].append(dict(box=list(BOX), confidence=.95))
    reference["frames"][0]["other_people"] = [dict(identity="person-2", box=list(BOX))]
    _, _, judgments = checked(frames, reference, window, {100: ["target", "other_person"]})
    for condition in CONDITIONS:
        assert make_suppressions(condition, judgments, frames) == []


def test_unknown_oracle_condition_is_rejected():
    frames, _, judgments = oracle_fixture()
    with pytest.raises(ValueError):
        make_suppressions("select-whichever-recovers", judgments, frames)


def decision(frame, *, phase="tracking", target_id=10, status="accepted"):
    return dict(sequence=frame["sequence"], status=status,
                detections=[dict(deepcopy(d), track_id=10 + index) for index, d in enumerate(frame["detections"])],
                preview=dict(phase=phase, target_id=target_id), consumer=dict(valid=phase == "tracking"))


def test_evaluation_keeps_misses_weak_selection_errors_and_unknowns_distinct():
    frames, reference, window = data(9)
    frames[0]["detections"] = []
    frames[1]["detections"][0]["confidence"] = .4
    reference["frames"][5]["other_people"] = [dict(identity="person-2", box=list(OTHER_BOX))]
    reference["frames"][8]["target"].update(visibility="uncertain", spatial_quality="uncertain", box=None)
    classes = {104: ["unknown"], 105: ["other_person"], 106: ["not_person"], 108: ["unknown"]}
    refs, _, judgments = checked(frames, reference, window, classes)
    run = dict(events=[], frame_decisions=[decision(frame,
               phase="paused" if i in (0, 1, 2) else "tracking",
               status="not_analyzed" if i == 7 else "accepted") for i, frame in enumerate(frames)])
    result = evaluate_replay(run, refs, judgments, start=10., end=10.4)
    assert [row["outcome"] for row in result["rows"]] == [
        "no_reviewed_target_detection", "only_weak_reviewed_target_detection", "strong_reviewed_target_not_selected",
        "selected_target_by_review", "selected_unknown_by_review", "selected_other_person_by_review",
        "selected_not_person_by_review", "not_admitted", "reference_uncertain_or_not_visible"]
    assert result["counts"]["reference_source_images"] == 9
    assert result["counts"]["clear_visible_source_images"] == 8
    assert result["counts"]["admitted_reference_images"] == 8
    assert result["counts"]["eligible_admitted_images"] == 7
    assert result["counts"]["selected_target_by_review"] == 1
    assert result["counts"]["selected_unknown_by_review"] == 1


def test_filtered_selection_uses_source_index_and_does_not_hide_original_detector_coverage():
    frames, refs, judgments = oracle_fixture()
    row = decision(frames[0], target_id=14)
    row.update(preview_detection_indices=[1, 4, 5],
               preview_detections=[deepcopy(row["detections"][i]) for i in (1, 4, 5)])
    result = evaluate_replay(dict(events=[], frame_decisions=[row]), refs, judgments, start=10., end=10.1)
    evaluated = result["rows"][0]
    assert evaluated["selected_detection_index"] == 4
    assert evaluated["selected_classification"] == "other_person"
    assert evaluated["target_detection_indices"] == [0, 1, 2]
    assert evaluated["outcome"] == "selected_other_person_by_review"


@pytest.mark.parametrize("has_weak_target,unknown_score,expected", [
    (False, .9, "unresolved_candidate_review"),
    (False, .4, "unresolved_candidate_review"),
    (True, .9, "unresolved_strong_candidate_review"),
    (True, .4, "only_weak_reviewed_target_detection"),
])
def test_unselected_unknown_candidates_are_not_silently_counted_as_detector_misses(has_weak_target, unknown_score, expected):
    frames, reference, window = data(1)
    frames[0]["detections"] = ([dict(box=list(BOX), confidence=.4)] if has_weak_target else [])
    frames[0]["detections"].append(dict(box=list(OTHER_BOX), confidence=unknown_score))
    classes = {100: (["target"] if has_weak_target else []) + ["unknown"]}
    refs, _, judgments = checked(frames, reference, window, classes)
    result = evaluate_replay(dict(events=[], frame_decisions=[decision(frames[0], phase="paused")]),
                             refs, judgments, start=10., end=10.1)
    assert result["rows"][0]["outcome"] == expected
    assert result["counts"]["eligible_admitted_images"] == 1
    assert result["counts"].get("no_reviewed_target_detection", 0) == 0
    assert result["counts"][expected] == 1


def test_duplicate_selected_ids_are_not_counted_as_one_valid_observation():
    frames, refs, judgments = oracle_fixture()
    row = decision(frames[0])
    row["detections"][1]["track_id"] = 10
    with pytest.raises(ValueError, match="multiple current observations"):
        evaluate_replay(dict(events=[], frame_decisions=[row]), refs, judgments, start=10., end=10.1)


def test_durations_are_clipped_to_review_span_and_remain_software_metrics():
    frames, reference, window = data(1)
    refs, _, judgments = checked(frames, reference, window)
    events = [dict(kind="preview_transition", at=9., phase="tracking"),
              dict(kind="consumer_transition", at=9., valid=True),
              dict(kind="preview_transition", at=10.2, phase="paused"),
              dict(kind="consumer_transition", at=10.25, valid=False),
              dict(kind="preview_transition", at=10.5, phase="stopped"),
              dict(kind="preview_transition", at=11.1, phase="idle")]
    result = evaluate_replay(dict(events=events, frame_decisions=[decision(frames[0])]), refs, judgments,
                             start=10., end=11.)
    assert result["phase_seconds"] == pytest.approx(dict(tracking=.2, paused=.3, stopped=.5))
    assert result["dry_valid_seconds"] == pytest.approx(.25)
    assert len(result["terminal_stops"]) == 1
    assert "not human-validated" in result["meaning"]
