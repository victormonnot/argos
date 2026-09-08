"""Image-law signs, damping and admission; these tests do not simulate flight."""
import math

import pytest

from argos.guidance.image_framing import AXIS_LIMITS, AXIS_SLEW_PER_SECOND, FramingLaw


def box(cx=.5, cy=.5, width=.1, height=.2):
    return [cx - width / 2, cy - height / 2, width, height]


def test_engagement_captures_current_framing_without_forward_jump():
    law = FramingLaw()
    law.start(box(height=.17), 1.)
    assert law.reference_height == law.height == .17
    assert law.update(box(height=.17), 1.2) == {"forward": 0., "right": 0., "up": 0., "yaw": 0.}


@pytest.mark.parametrize("cx,cy,yaw_sign,up_sign", [
    (.7, .7, 1, -1), (.3, .3, -1, 1), (.7, .3, 1, 1), (.3, .7, -1, -1),
])
def test_centering_uses_body_camera_yaw_and_vertical_signs(cx, cy, yaw_sign, up_sign):
    law = FramingLaw()
    law.start(box(), 1.)
    axes = law.update(box(cx=cx, cy=cy), 1.2)
    assert math.copysign(1, axes["yaw"]) == yaw_sign
    assert math.copysign(1, axes["up"]) == up_sign
    assert axes["right"] == 0 and axes["forward"] == 0
    assert law.error_x == pytest.approx(2 * (cx - .5))
    assert law.error_y == pytest.approx(2 * (cy - .5))


def test_small_pointing_error_and_pitch_coupling_do_not_chase_vertical_translation():
    law = FramingLaw()
    law.start(box(), 1.)
    axes = law.update(box(cx=.51, cy=.536), 1.2)
    assert axes["yaw"] == 0 and axes["up"] == 0


def test_closer_farther_change_only_the_reference_and_have_no_derivative_kick():
    law = FramingLaw()
    law.start(box(), 1.)
    assert law.adjust("closer") == pytest.approx(.22)
    axes = law.update(box(), 1.2)
    assert axes["forward"] == pytest.approx(.35 * math.log(1.1))
    law.adjust("farther")
    axes = law.update(box(), 1.4)
    assert axes["forward"] == pytest.approx(0, abs=1e-12)
    law.adjust("farther")
    assert law.update(box(), 1.6)["forward"] < 0


def test_reference_adjustments_are_bounded():
    law = FramingLaw()
    law.start(box(), 1.)
    for _ in range(50):
        law.adjust("closer")
    assert law.reference_height == .45
    for _ in range(50):
        law.adjust("farther")
    assert law.reference_height == .08


def test_growth_brakes_before_the_reference_height_is_reached():
    law = FramingLaw()
    law.start(box(height=.18), 1.)
    for _ in range(3):
        law.adjust("closer")
    for step, height in enumerate((.182, .19, .20, .21), 1):
        axes = law.update(box(height=height), 1 + step * .2)
    assert law.height < law.reference_height  # Proportional-only control would approach.
    assert axes["forward"] < 0  # Growing image size supplies a braking command.


def test_detector_height_jitter_is_filtered_instead_of_becoming_large_pitch_commands():
    law = FramingLaw()
    law.start(box(), 1.)
    outputs = [law.update(box(height=.2 if step % 2 else .205), 1 + step * .2)["forward"]
               for step in range(1, 31)]
    assert max(abs(value) for value in outputs) < .04


@pytest.mark.parametrize("changed", [box(cx=.63), box(cy=.63), [0, .4, 1, .2]])
def test_losing_alignment_or_clipping_inhibits_forward_immediately(changed):
    law = FramingLaw()
    law.start(box(), 1.)
    law.adjust("closer")
    assert law.update(box(), 1.2)["forward"] > 0
    assert law.update(changed, 1.201)["forward"] == 0


def test_returning_after_clipping_does_not_differentiate_truncated_height():
    law = FramingLaw()
    law.start(box(), 1.)
    law.update([.45, 0, .1, .1], 1.2)
    assert law.update(box(), 1.4)["forward"] == pytest.approx(0, abs=1e-12)


def test_outputs_remain_bounded_and_slew_at_most_once_per_observation():
    law = FramingLaw()
    law.start(box(), 1.)
    previous = dict.fromkeys(AXIS_LIMITS, 0.)
    for step in range(1, 31):
        current = law.update(box(cx=.9 if step < 15 else .1, cy=.8), 1 + step * .2)
        for axis, limit in AXIS_LIMITS.items():
            assert abs(current[axis]) <= limit + 1e-12
        for axis in ("yaw", "up"):
            assert abs(current[axis] - previous[axis]) <= AXIS_SLEW_PER_SECOND[axis] * .2 + 1e-12
        previous = current


def test_long_update_gap_does_not_allow_a_large_slew_step():
    law = FramingLaw()
    law.start(box(), 1.)
    axes = law.update(box(cx=.9, cy=.8), 100.)
    assert axes["yaw"] == pytest.approx(.9 * .25)
    assert axes["up"] == pytest.approx(-.4 * .25)


def test_pause_preserves_reference_and_resumes_without_old_height_derivative():
    law = FramingLaw()
    law.start(box(), 1.)
    reference = law.adjust("closer")
    assert law.update(box(height=.27), 1.2)["forward"] < 0
    law.pause()
    law.pause()  # Repeated neutralization cannot refresh the observation clock.
    assert law.reference_height == reference
    axes = law.update(box(height=.21), 1.4)
    assert axes["forward"] == pytest.approx(.35 * math.log(reference / .21))
    assert axes["right"] == axes["up"] == axes["yaw"] == 0


def test_pause_discards_nonzero_command_history_before_resuming():
    law = FramingLaw()
    law.start(box(), 1.)
    for step in range(1, 6):
        previous = law.update(box(cx=.9, cy=.8), 1 + step * .2)
    assert previous["yaw"] == .5 and previous["up"] == -.3
    law.pause()
    axes = law.update(box(), 2.2)
    assert all(value == 0 for value in axes.values())


def test_pause_keeps_strict_timestamp_order_and_short_interval_slew_limit():
    law = FramingLaw()
    law.start(box(), 1.)
    law.update(box(cx=.9), 1.2)
    law.pause()
    for stamp in (1., 1.2):
        with pytest.raises(ValueError, match="strictly increase"):
            law.update(box(), stamp)
    assert law.update(box(cx=.9), 1.3)["yaw"] == pytest.approx(.9 * .1)


def test_pause_resume_after_long_gap_still_caps_slew_from_zero():
    law = FramingLaw()
    law.start(box(), 1.)
    law.update(box(cx=.9, cy=.8), 1.2)
    law.pause()
    axes = law.update(box(cx=.1, cy=.2), 100.)
    assert axes["yaw"] == pytest.approx(-.9 * .25)
    assert axes["up"] == pytest.approx(.4 * .25)


def test_pause_does_not_start_an_uninitialized_law():
    law = FramingLaw()
    law.pause()
    assert law.reference_height is None
    with pytest.raises(RuntimeError, match="not started"):
        law.update(box(), 1.)


def test_reset_forgets_reference_derivative_and_outputs():
    law = FramingLaw()
    law.start(box(), 1.)
    law.update(box(cx=.7, height=.3), 1.2)
    law.reset()
    assert law.reference_height is None and law.height is None
    assert law.error_x == law.error_y == 0
    with pytest.raises(RuntimeError, match="not started"):
        law.update(box(), 2.)
    with pytest.raises(RuntimeError, match="not started"):
        law.adjust("closer")
    law.start(box(height=.3), 0.)
    assert all(value == 0 for value in law.update(box(height=.3), .2).values())


@pytest.mark.parametrize("invalid", [None, [], [0, 0, 0, .2], [0, 0, .2, -1],
    [0, 0, .2, float("nan")], [0, 0, .2, float("inf")],
    [True, 0, .2, .2], [.9, 0, .2, .2], [0, .9, .2, .2], [0, 0, .2, 10**400]])
def test_invalid_boxes_leave_previous_law_state_unchanged(invalid):
    law = FramingLaw()
    law.start(box(), 1.)
    with pytest.raises(ValueError):
        law.update(invalid, 1.2)
    assert law.reference_height == law.height == .2
    assert all(value == 0 for value in law.update(box(), 1.2).values())


@pytest.mark.parametrize("stamp", [None, True, -1, 1., .5, float("nan"), float("inf"), 10**400])
def test_bad_or_repeated_timestamps_never_accumulate_control(stamp):
    law = FramingLaw()
    law.start(box(), 1.)
    with pytest.raises(ValueError):
        law.update(box(cx=.7), stamp)
    assert all(value == 0 for value in law.update(box(), 1.2).values())


@pytest.mark.parametrize("height", [.07, .46])
def test_engagement_refuses_out_of_range_height_instead_of_approaching_silently(height):
    law = FramingLaw()
    law.start(box(), 1.)
    with pytest.raises(ValueError, match="between 8% and 45%"):
        law.start(box(height=height), 2.)
    assert law.reference_height == .2


def test_tiny_positive_timestamp_step_keeps_all_outputs_finite():
    law = FramingLaw()
    law.start(box(), 0.)
    result = law.update(box(height=.3), 1e-300)
    assert all(math.isfinite(value) for value in result.values())


@pytest.mark.parametrize("direction", ["nearer", "", None, 1])
def test_unknown_reference_adjustment_is_rejected(direction):
    law = FramingLaw()
    law.start(box(), 1.)
    with pytest.raises(ValueError):
        law.adjust(direction)
    assert law.reference_height == .2
