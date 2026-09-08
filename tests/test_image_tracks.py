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
    assert result[0]["track_id"] == old_id
    assert result[1]["track_id"] != old_id
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
