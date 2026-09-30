from copy import deepcopy
import pytest

from argos.backends.distance_source import DistanceValidator
from argos.backends.vision_bench_source import PreviewError
from test_yaw_stream_source import snapshot, pause
from test_apparent_distance import box


def observation(state, *, height=.5, identity=7, sequence=None):
    p = state["yaw_preview"]
    return dict(run_id=p["run_id"], video_id=p["video_id"],
                sequence=p["frame_sequence"] if sequence is None else sequence,
                received_at=p["frame_received_at"],
                detections=[dict(track_id=identity, confidence=.9, box=box(height))])


def test_explicit_distance_reference_only_no_pitch_on_default_or_yaw_mode():
    state = snapshot()
    validator = DistanceValidator()
    yaw = validator.validate(state, 100., 100.01, observation=observation(state))
    assert yaw.mode == "Y" and not yaw.pitch_valid and yaw.pitch == 0
    state = snapshot(at=10.02)
    distance = validator.validate(state, 100.02, 100.03, explicit_selection=True,
                                  mode="D", observation=observation(state))
    assert distance.valid and distance.pitch_valid and distance.pitch == 0
    assert distance.reference_height == .5
    state = snapshot(at=10.04)
    yaw = validator.validate(state, 100.04, 100.05, explicit_selection=True,
                             mode="Y", observation=observation(state))
    assert yaw.mode == "Y" and not yaw.pitch_valid


@pytest.mark.parametrize("change", [dict(identity=9), dict(sequence=43)])
def test_wrong_frame_or_person_cannot_supply_distance(change):
    state = snapshot()
    with pytest.raises(PreviewError, match="reference"):
        DistanceValidator().validate(state, 100., 100.01, explicit_selection=True,
                                     mode="D", observation=observation(state, **change))


def test_loss_withdraws_pitch_as_well_as_yaw_without_erasing_reference():
    state = snapshot()
    validator = DistanceValidator()
    validator.validate(state, 100., 100.01, explicit_selection=True,
                       mode="D", observation=observation(state))
    lost = pause(snapshot(at=10.1, received=10., sequence=43))
    demand = validator.validate(lost, 100.1, 100.11)
    assert not demand.valid and not demand.pitch_valid and demand.pitch == 0
    assert demand.reference_height == .5


def test_selection_change_never_reuses_old_distance_reference():
    state = snapshot()
    validator = DistanceValidator()
    validator.validate(state, 100., 100.01, explicit_selection=True,
                       mode="D", observation=observation(state))
    changed = snapshot(at=10.1, received=10., sequence=43)
    changed["yaw_preview"].update(selection_epoch=2, target_id=9)
    demand = validator.validate(changed, 100.1, 100.11,
                                observation=observation(changed, identity=9))
    assert demand.valid and not demand.pitch_valid and demand.reference_height is None


def test_copy_can_fail_without_mutating_published_controller():
    state = snapshot()
    validator = DistanceValidator()
    validator.validate(state, 100., 100.01, explicit_selection=True,
                       mode="D", observation=observation(state))
    candidate = deepcopy(validator)
    with pytest.raises(PreviewError):
        candidate.validate(state, 100., 100.01, explicit_selection=True,
                           mode="D", observation=observation(state, height=.95))
    assert validator.law.reference == .5 and validator.law.valid
