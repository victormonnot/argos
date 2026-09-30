import math
import pytest

from argos.guidance.apparent_distance import ApparentDistanceLaw, PITCH_LIMIT


def box(height=.5, *, center=.5, aspect=.4):
    width = aspect * height
    return [center - width / 2, .5 - height / 2, width, height]


def test_reference_zero_then_forward_and_back_with_limits():
    for height, sign in ((.35, 1), (.65, -1)):
        law = ApparentDistanceLaw()
        law.start(box(), 0.)
        assert law.valid and law.value == 0
        previous = 0.
        for i in range(1, 35):
            value = law.update(box(height), i * .1)
            assert abs(value) <= PITCH_LIMIT + 1e-10
            if law.valid and value * previous >= 0 and abs(value) > abs(previous):
                assert abs(value - previous) <= .010001
            previous = value
        assert law.valid and value * sign > .02


def test_repeated_frame_never_confirms_after_gap():
    law = ApparentDistanceLaw()
    law.start(box(), 0.)
    law.pause()
    for _ in range(10):
        assert law.update(box(), 0.) == 0
        assert not law.valid
    law.update(box(), .1)
    assert not law.valid
    law.update(box(), .2)
    assert law.valid


@pytest.mark.parametrize("bad", [[0., .1, .2, .5], box(center=.9), box(.9), box(aspect=.7)])
def test_clipping_alignment_and_changed_pose_withdraw(bad):
    law = ApparentDistanceLaw()
    law.start(box(), 0.)
    for i in range(1, 10):
        law.update(box(.4), i * .1)
    law.update(bad, 1.)
    assert law.value == 0 and not law.valid


def test_long_gap_and_height_jump_reset_derivative_and_need_fresh_confirmation():
    law = ApparentDistanceLaw()
    law.start(box(), 1.)
    law.update(box(.3), 1.1)
    assert not law.valid and law.value == 0
    law.update(box(.3), 1.2)
    assert not law.valid
    law.update(box(.3), 1.3)
    assert law.valid
    law.update(box(.3), 2.)
    assert not law.valid and law.rate == 0


def test_damping_brakes_approach_before_reaching_reference():
    static, approaching = ApparentDistanceLaw(), ApparentDistanceLaw()
    for law in (static, approaching):
        law.start(box(), 0.)
        for i in range(1, 20):
            law.update(box(.38), i * .1)
    static.update(box(.38), 2.)
    approaching.update(box(.42), 2.)
    assert approaching.value < static.value


def settled_law(height):
    law = ApparentDistanceLaw()
    law.start(box(), 0.)
    for i in range(1, 51):
        law.update(box(height), i * .1)
    assert law.valid
    return law


@pytest.mark.parametrize("height,closer_to_reference", [(.35, .385), (.65, .59)])
def test_approach_reduces_existing_correction_without_old_slew_delay(height, closer_to_reference):
    law = settled_law(height)
    previous = law.value
    value = law.update(box(closer_to_reference), 5.1)
    assert law.valid
    assert value * previous > 0  # Still asks to approach the reference.
    assert abs(value) < abs(previous) - .02  # Old limiter allowed only .01.
    assert abs(value) <= PITCH_LIMIT


@pytest.mark.parametrize("height,next_height", [(.38, .456), (.65, .542)])
def test_braking_reverses_on_next_analysis_before_reference_crossing(height, next_height):
    law = settled_law(height)
    previous = law.value
    value = law.update(box(next_height), 5.1)
    assert law.valid
    assert (height - .5) * (next_height - .5) > 0
    assert value * previous < 0  # Brake now, not after ramping the old sign down.
    assert abs(value) <= PITCH_LIMIT
    assert law.update(box(next_height), 5.1) == value  # Same image adds no action.


@pytest.mark.parametrize("previous", [-PITCH_LIMIT, PITCH_LIMIT])
def test_neutral_filtered_request_releases_previous_command_immediately(previous):
    law = ApparentDistanceLaw()
    law.start(box(), 0.)
    # Isolate the neutral-request boundary with a previous output still applied.
    law.value = previous
    assert law.update(box(), .1) == 0
    assert law.valid


@pytest.mark.parametrize("height", [.4, .6])
def test_buildup_from_zero_stays_gentle_and_caps_time_credit(height):
    law = ApparentDistanceLaw()
    law.start(box(), 0.)
    first = law.update(box(height), .3)
    assert law.valid
    assert 0 < abs(first) <= .01500001
    second = law.update(box(height), .4)
    assert second * first > 0
    assert abs(second) > abs(first)
    assert abs(second - first) <= .01000001


@pytest.mark.parametrize("at", [True, math.nan, math.inf, -1.])
def test_bad_times_refused(at):
    with pytest.raises(ValueError):
        ApparentDistanceLaw().start(box(), at)


def test_regression_and_nonfinite_boxes_refused():
    law = ApparentDistanceLaw()
    law.start(box(), 1.)
    with pytest.raises(ValueError):
        law.update(box(), .9)
    with pytest.raises(ValueError):
        law.update([.2, .2, math.nan, .3], 1.1)
