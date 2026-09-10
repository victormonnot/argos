"""Pilot-throttle guidance authority and lifecycle, without a vehicle source."""

import pytest

from argos.console.framing import DETECTION_PAUSE, FramingControl
from argos.guidance.image_framing import FramingLaw
from test_console_framing import CONTEXT, observation, zero
from test_image_framing import box


def engaged():
    control = FramingControl(enabled=True)
    control.observe(observation())
    control.select(7, CONTEXT, 10.)
    control.engage(10., profile="pilot_throttle")
    return control


@pytest.mark.parametrize("cy", [.25, .75])
def test_pilot_throttle_law_keeps_vertical_zero_and_inhibits_misaligned_approach(cy):
    law = FramingLaw()
    law.start(box(), 1., vertical_control=False)
    law.adjust("closer")
    assert law.update(box(), 1.2)["forward"] > 0
    output = law.update(box(cx=.7, cy=cy), 1.201)
    assert output["yaw"] > 0
    assert output["up"] == output["forward"] == output["right"] == 0
    assert abs(law.error_y) == pytest.approx(.5)
    law.pause()
    assert law.update(box(cy=cy), 1.4)["up"] == 0
    # Explicitly restarting the default full profile restores vertical control;
    # resetting a shared engagement never silently restores it while active.
    law.start(box(), 2.)
    assert law.update(box(cy=cy), 2.2)["up"] != 0


def test_pilot_throttle_preserves_full_profile_horizontal_law_for_identical_images():
    shared, full = FramingLaw(), FramingLaw()
    shared.start(box(), 1., vertical_control=False)
    full.start(box(), 1.)
    for law in (shared, full):
        law.adjust("closer")
    for step, measurement in enumerate((box(height=.18), box(height=.19, cy=.6),
                                        box(height=.2, cy=.65), box(height=.21)), 1):
        pilot = shared.update(measurement, 1 + step * .2)
        auto = full.update(measurement, 1 + step * .2)
        assert pilot["up"] == 0
        assert {axis: pilot[axis] for axis in ("forward", "right", "yaw")} == {
            axis: auto[axis] for axis in ("forward", "right", "yaw")}


@pytest.mark.parametrize("invalid", [None, "", "radio", "FULL", True, [], {}])
def test_unknown_profile_cannot_mutate_selected_target(invalid):
    control = FramingControl(enabled=True)
    control.observe(observation())
    control.select(7, CONTEXT, 10.)
    before = control.state(10.)
    with pytest.raises(ValueError, match="full or pilot_throttle"):
        control.engage(10., profile=invalid)
    assert control.state(10.) == before


@pytest.mark.parametrize("vertical", [1e-12, -1e-12, .3, -.3])
def test_shared_boundary_rejects_any_vertical_output_from_broken_guidance(vertical):
    control = engaged()
    control._law.update = lambda *args: {**zero(), "up": vertical}
    control.observe(observation(10.2, 2))
    control.tick(10.2)
    assert control.phase == "takeover"
    assert control.axes(10.2) == zero()
    assert control.last_loss["profile"] == "pilot_throttle"
    assert "calculation failed" in control.last_loss["reason"]


def test_shared_pause_preserves_profile_reference_and_zero_vertical_on_recovery():
    control = engaged()
    control.adjust("closer", 10.1)
    reference = control.state(10.1)["reference_height"]
    control.observe(observation(10.2, 2, detections=[]))
    control.tick(10.2)
    paused = control.state(10.2)
    assert paused["profile"] == "pilot_throttle" and paused["paused"]
    assert "keep controlling throttle" in paused["reason"]
    assert paused["axes"] == zero()
    control.observe(observation(10.4, 3))
    control.tick(10.4)
    recovered = control.state(10.4)
    assert recovered["active"] and not recovered["paused"]
    assert recovered["reference_height"] == reference
    assert recovered["axes"]["up"] == 0


def test_shared_loss_latches_profile_even_after_clear_and_never_auto_reengages():
    control = engaged()
    control.observe(observation(10.2, 2, detections=[]))
    control.tick(10.2)
    deadline = 10.2 + DETECTION_PAUSE
    control.observe(observation(deadline, 3))
    control.tick(deadline)
    assert control.phase == "takeover"
    loss = control.last_loss
    assert loss["profile"] == "pilot_throttle"
    control.observe(observation(deadline + .1, 4))
    control.tick(deadline + .1)
    assert control.phase == "takeover" and control.axes(deadline + .1) == zero()
    control.clear("Flight mode changed; select and engage framing again")
    assert control.phase == "idle" and control.profile == "full"
    assert control.last_loss == loss


@pytest.mark.parametrize("invalid", [None, 0, 1, "false"])
def test_invalid_vertical_authority_does_not_change_running_law(invalid):
    law = FramingLaw()
    law.start(box(), 1., vertical_control=False)
    with pytest.raises(ValueError, match="explicitly enabled or disabled"):
        law.start(box(height=.3), 2., vertical_control=invalid)
    assert law.reference_height == .2
    assert law.update(box(cy=.7), 1.2)["up"] == 0
