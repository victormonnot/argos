"""Passive image associations never manufacture fresh observations."""
import pytest

from argos.perception.image_tracks import ImageTracker


def person(x=.1, y=.2, w=.2, h=.5, confidence=.8):
    return {"box": [x, y, w, h], "confidence": confidence}


def test_nearby_detections_keep_ids_even_when_order_changes():
    tracker = ImageTracker()
    first = tracker.update([person(), person(.6)], 1)
    second = tracker.update([person(.61), person(.11)], 1.1)
    assert [box["track_id"] for box in second] == [first[1]["track_id"], first[0]["track_id"]]
    assert second[0]["box"] == person(.61)["box"]


def test_empty_frames_do_not_emit_predicted_boxes_or_refresh_detection_ttl():
    tracker = ImageTracker()
    old_id = tracker.update([person()], 1)[0]["track_id"]
    assert tracker.update([], 1.2) == []
    assert tracker.update([], 1.6) == []
    assert tracker.update([person()], 1.71)[0]["track_id"] != old_id


def test_short_occlusion_can_associate_again_but_long_gap_cannot():
    tracker = ImageTracker()
    old_id = tracker.update([person()], 1)[0]["track_id"]
    tracker.update([], 1.1)
    assert tracker.update([person(.11)], 1.6)[0]["track_id"] == old_id
    assert tracker.update([person(.11)], 2.31)[0]["track_id"] != old_id


def test_each_track_can_match_only_one_detection():
    tracker = ImageTracker()
    old_id = tracker.update([person()], 1)[0]["track_id"]
    result = tracker.update([person(.11), person(.12)], 1.1)
    assert all(item["track_id"] != old_id for item in result)
    assert len({item["track_id"] for item in result}) == 2


def test_disjoint_box_is_not_associated_to_the_old_person():
    tracker = ImageTracker()
    old_id = tracker.update([person()], 1)[0]["track_id"]
    assert tracker.update([person(.7)], 1.1)[0]["track_id"] != old_id


def test_narrow_walking_person_can_move_and_change_pose_between_detections():
    tracker = ImageTracker()
    old_id = tracker.update([person(.3, .3, .06, .2)], 1)[0]["track_id"]
    measured = person(.347, .302, .04, .198)
    result = tracker.update([measured], 1.23)
    assert result == [{**measured, "track_id": old_id}]


@pytest.mark.parametrize("positions", [[.335], [.331, .339]])
def test_nearby_fallback_refuses_ambiguous_people_crossing(positions):
    tracker = ImageTracker()
    original = tracker.update([person(.3, .3, .04, .2), person(.37, .3, .04, .2)], 1)
    previous_ids = {item["track_id"] for item in original}
    result = tracker.update([person(x, .3, .04, .2) for x in positions], 1.2)
    assert all(item["track_id"] not in previous_ids for item in result)
    assert len({item["track_id"] for item in result}) == len(result)


@pytest.mark.parametrize("next_person,dt", [
    (person(.35, .3, .04, .2), .4),  # Nearby but too old for weak association.
    (person(.4, .3, .04, .2), .2),  # Large horizontal jump.
    (person(.35, .4, .04, .2), .2),  # Large vertical jump.
    (person(.35, .3, .04, .4), .2),  # Incompatible apparent height.
])
def test_nearby_fallback_keeps_time_displacement_and_scale_gates(next_person, dt):
    tracker = ImageTracker()
    old_id = tracker.update([person(.3, .3, .04, .2)], 1)[0]["track_id"]
    assert tracker.update([next_person], 1 + dt)[0]["track_id"] != old_id


def test_reset_discards_associations_and_accepts_new_clock_origin():
    tracker = ImageTracker()
    old_id = tracker.update([person()], 99)[0]["track_id"]
    tracker.reset()
    assert tracker.update([person()], 0)[0]["track_id"] != old_id


def test_input_and_output_mutation_cannot_change_stored_boxes():
    tracker = ImageTracker()
    original = person()
    result = tracker.update([original], 1)
    old_id = result[0]["track_id"]
    original["box"][0] = .7
    result[0]["box"][0] = .7
    assert tracker.update([person(.11)], 1.1)[0]["track_id"] == old_id


@pytest.mark.parametrize("stamp", [True, None, -1, 1, .5, float("nan"), float("inf"), 10**400])
def test_bad_or_repeated_timestamps_do_not_refresh_tracks(stamp):
    tracker = ImageTracker()
    old_id = tracker.update([person()], 1)[0]["track_id"]
    with pytest.raises(ValueError):
        tracker.update([person()], stamp)
    assert tracker.update([person()], 1.71)[0]["track_id"] != old_id


@pytest.mark.parametrize("bad", [None, {}, [None], [person()] * 17,
    [{"box": [0, 0, 0, .5], "confidence": .8}],
    [{"box": [.9, 0, .5, .5], "confidence": .8}],
    [{"box": [0, 0, float("nan"), .5], "confidence": .8}],
    [{"box": [True, 0, .2, .5], "confidence": .8}],
    [{"box": [0, 0, .2, .5], "confidence": float("inf")}],
])
def test_invalid_detections_leave_association_state_unchanged(bad):
    tracker = ImageTracker()
    old_id = tracker.update([person()], 1)[0]["track_id"]
    with pytest.raises(ValueError):
        tracker.update(bad, 1.1)
    assert tracker.update([person()], 1.2)[0]["track_id"] == old_id


def test_association_memory_stays_bounded_when_new_people_replace_previous_ones():
    tracker = ImageTracker()
    for frame in range(20):
        offset = .1 if frame % 2 else 0
        detections = [person((index % 4) * .24 + offset, (index // 4) * .24, .05, .05)
                      for index in range(16)]
        tracker.update(detections, frame / 10)
        assert len(tracker._tracks) <= 16


def appearance(score=1., basis=0):
    values = [0.] * 208
    values[basis] = score
    values[basis + 1] = (1 - score ** 2) ** .5
    return values


def narrow(x=.3, confidence=.8):
    return person(x, .3, .04, .2, confidence)


def test_appearance_can_associate_bounded_yaw_displacement_without_changing_measurement():
    tracker = ImageTracker()
    old = tracker.update([narrow()], 1, appearances=[appearance()])[0]
    measured = narrow(.4, .72)
    result = tracker.update([measured], 1.2, appearances=[appearance(.98)])
    assert result == [{**measured, "track_id": old["track_id"]}]


@pytest.mark.parametrize("box,stamp,descriptor", [
    (narrow(.4), 1.2, appearance(.94)),
    (narrow(.45), 1.2, appearance()),  # Larger than the bounded motion gate.
    (person(.4, .4, .04, .2), 1.2, appearance()),
    (person(.4, .3, .04, .4), 1.2, appearance()),
    (narrow(.4), 1.36, appearance()),
    (narrow(.4), 1.2, None),
    (narrow(.4, .4), 1.2, appearance()),  # Weak boxes get no expanded match.
])
def test_expanded_association_refuses_weak_evidence_or_excess_motion(box, stamp, descriptor):
    tracker = ImageTracker()
    old = tracker.update([narrow()], 1, appearances=[appearance()])[0]["track_id"]
    assert tracker.update([box], stamp, appearances=[descriptor])[0]["track_id"] != old


def test_expanded_appearance_needs_margin_against_competing_current_boxes():
    tracker = ImageTracker()
    old = tracker.update([narrow()], 1, appearances=[appearance()])[0]["track_id"]
    result = tracker.update([narrow(.39), narrow(.41)], 1.2,
                            appearances=[appearance(.99), appearance(.94)])
    assert all(item["track_id"] != old for item in result)


def test_expanded_appearance_needs_margin_against_competing_old_tracks():
    tracker = ImageTracker()
    old = tracker.update([narrow(.2), narrow(.4)], 1,
                         appearances=[appearance(), appearance(.99)])
    result = tracker.update([narrow(.3)], 1.2, appearances=[appearance()])
    assert result[0]["track_id"] not in {item["track_id"] for item in old}


def test_unique_geometry_accepts_moderate_appearance_but_vetoes_gross_contradiction():
    for score, retains in [(.9, True), (.7, False)]:
        tracker = ImageTracker()
        old = tracker.update([person()], 1, appearances=[appearance()])[0]["track_id"]
        result = tracker.update([person(.11)], 1.2, appearances=[appearance(score)])
        assert (result[0]["track_id"] == old) is retains


def test_ambiguous_geometry_cannot_be_rescued_by_the_expanded_fallback():
    tracker = ImageTracker()
    old = tracker.update([person()], 1, appearances=[appearance()])[0]["track_id"]
    result = tracker.update([person(.11), person(.12)], 1.2,
                            appearances=[appearance(), appearance(.8)])
    assert all(item["track_id"] != old for item in result)


def test_individually_missing_appearance_allows_only_unique_geometry():
    tracker = ImageTracker()
    old = tracker.update([narrow()], 1, appearances=[appearance()])[0]["track_id"]
    assert tracker.update([narrow(.31)], 1.2, appearances=[None])[0]["track_id"] == old
    assert tracker._tracks[old].appearance_at == 1
    assert tracker.update([narrow(.41)], 1.3, appearances=[None])[0]["track_id"] != old


@pytest.mark.parametrize("weak,descriptor", [(True, appearance()), (False, None)])
def test_weak_or_missing_evidence_cannot_extend_strong_appearance_lifetime(weak, descriptor):
    tracker = ImageTracker()
    old = tracker.update([narrow()], 1, appearances=[appearance()])[0]["track_id"]
    observed = narrow(.31, .4 if weak else .8)
    assert tracker.update([observed], 1.2, appearances=[descriptor])[0]["track_id"] == old
    assert tracker._tracks[old].appearance_at == 1
    assert tracker.update([narrow(.41)], 1.4, appearances=[appearance()])[0]["track_id"] != old


def test_stale_appearance_is_unavailable_to_veto_ordinary_geometry():
    tracker = ImageTracker()
    old = tracker.update([person()], 1, appearances=[appearance()])[0]["track_id"]
    result = tracker.update([person(.11)], 1.4, appearances=[appearance(0.)])
    assert result[0]["track_id"] == old


def test_expanded_matching_does_not_bridge_an_empty_accepted_frame():
    tracker = ImageTracker()
    old = tracker.update([narrow()], 1, appearances=[appearance()])[0]["track_id"]
    assert tracker.update([], 1.1, appearances=[]) == []
    assert tracker.update([narrow(.4)], 1.2, appearances=[appearance()])[0]["track_id"] != old


def test_appearance_does_not_extend_detection_ttl_or_emit_prediction():
    tracker = ImageTracker()
    old = tracker.update([person()], 1, appearances=[appearance()])[0]["track_id"]
    assert tracker.update([], 1.6, appearances=[]) == []
    assert tracker.update([person()], 1.71, appearances=[appearance()])[0]["track_id"] != old


def test_new_weak_nested_box_cannot_steal_a_strong_established_id():
    tracker = ImageTracker()
    old = tracker.update([person()], 1, appearances=[appearance()])[0]["track_id"]
    result = tracker.update([person(.11, confidence=.4), person(.12)], 1.2,
                            appearances=[appearance(), appearance()])
    weak_id = result[0]["track_id"]
    assert result[1]["track_id"] == old
    assert weak_id != old and weak_id not in tracker._tracks
    assert tracker.update([person(.13)], 1.3, appearances=[appearance()])[0]["track_id"] == old


def test_new_weak_boxes_are_visible_without_establishing_persistent_memory():
    tracker = ImageTracker()
    first = tracker.update([person(confidence=.4)], 1, appearances=[appearance()])
    second = tracker.update([person(confidence=.4)], 1.2, appearances=[appearance()])
    assert len(first) == len(second) == 1
    assert first[0]["track_id"] != second[0]["track_id"]
    assert tracker._tracks == {}


@pytest.mark.parametrize("bad", [None, [], [appearance(), None], [[0.] * 208],
                                      [[float("nan")] * 208], [[True] + [0.] * 207]])
def test_bad_appearance_metadata_is_atomic(bad):
    tracker = ImageTracker()
    old = tracker.update([narrow()], 1, appearances=[appearance()])[0]["track_id"]
    with pytest.raises(ValueError):
        tracker.update([narrow(.4)], 1.2, appearances=bad)
    assert tracker._last_at == 1
    assert tracker.update([narrow(.4)], 1.2, appearances=[appearance()])[0]["track_id"] == old


def test_input_descriptor_mutation_does_not_rewrite_the_reference():
    tracker = ImageTracker()
    descriptor = appearance()
    old = tracker.update([narrow()], 1, appearances=[descriptor])[0]["track_id"]
    descriptor[0], descriptor[1] = 0., 1.
    assert tracker.update([narrow(.4)], 1.2, appearances=[appearance()])[0]["track_id"] == old


def test_reset_discards_appearance_history_and_metadata_opt_in():
    tracker = ImageTracker()
    old = tracker.update([narrow()], 99, appearances=[appearance()])[0]["track_id"]
    tracker.reset()
    assert tracker.update([narrow()], 0)[0]["track_id"] != old
