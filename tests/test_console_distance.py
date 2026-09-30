"""The isolated apparent-distance source keeps owner, identity and time boundaries."""
from dataclasses import replace
import threading

import pytest

from test_console_yaw_assist import fly  # shared real-session fixture, no hardware
from argos.console.distance_source import LocalDistanceSource
from argos.console.distance_assist import DistanceAssistService
from argos.backends.vision_bench_source import PreviewError
from argos.backends.yaw_stream_source import YawValidator


def person(height=.5, *, identity=7, center=.5, confidence=.9, top=None):
    width = height * .2
    return {"track_id": identity,
            "box": [center - width / 2, .5 - height / 2 if top is None else top, width, height],
            "confidence": confidence}


@pytest.fixture
def local_distance(fly):
    fly["observe"](1., [person()])
    callbacks = []
    source = LocalDistanceSource(fly["session"], callbacks.append,
                                 clock=lambda: fly["now"][0] + 100.)
    source.publish()
    yield source, callbacks
    source.close()


def enable(source, callbacks, mode="D", generation=1):
    source.request_selection(f"1234abcd:{generation}", mode)
    assert len(callbacks) == 1
    callbacks.pop(0)()
    return source.snapshot()


def observe(fly, source, at, people=None):
    fly["observe"](at, [person()] if people is None else people)
    source.publish()
    return source.snapshot()


def test_mode_selection_captures_current_reference_and_keeps_baseline_yaw(fly, local_distance):
    source, callbacks = local_distance
    selected = enable(source, callbacks)
    assert not selected.error, source.metrics()["last_error"]
    assert selected.selection_result.success
    assert selected.demand.valid and selected.demand.pitch_valid
    assert selected.demand.reference_height == .5
    assert selected.demand.pitch == 0
    assert selected.demand.mode == "D"
    sample = observe(fly, source, 1.1, [person(.43)])
    assert sample.demand.reference_height == .5
    state = fly["session"].yaw_assist_state(refresh=False)
    baseline = YawValidator().validate(state, 101.1, 101.1)
    assert baseline.value == sample.demand.value
    assert baseline.deadline == sample.demand.deadline


def test_yaw_only_never_captures_pitch_reference(fly, local_distance):
    source, callbacks = local_distance
    sample = enable(source, callbacks, mode="Y")
    assert sample.demand.valid and not sample.demand.pitch_valid
    assert sample.demand.mode == "Y" and sample.demand.reference_height is None
    sample = observe(fly, source, 1.1, [person(.4)])
    assert sample.demand.pitch == 0 and not sample.demand.pitch_valid


def test_reference_changes_only_on_fresh_explicit_enable(fly, local_distance):
    source, callbacks = local_distance
    initial = enable(source, callbacks)
    for i, height in enumerate((.48, .46, .44, .42), 1):
        sample = observe(fly, source, 1. + .1 * i, [person(height)])
        assert sample.demand.reference_height == initial.demand.reference_height
    assert sample.demand.pitch > 0
    fly["now"][0] += .01
    selected = enable(source, callbacks, generation=2)
    assert not selected.error, source.metrics()["last_error"]
    assert selected.demand.reference_height == pytest.approx(.42)
    assert selected.demand.pitch == 0


def test_mode_and_token_callback_coalesce_as_one_transaction(fly, local_distance):
    source, callbacks = local_distance
    source.request_selection("1234abcd:1", "D")
    source.request_selection("1234abcd:2", "Y")
    assert len(callbacks) == 1
    callbacks.pop()()
    result = source.snapshot()
    assert result.selection_result.request_token == "1234abcd:2"
    assert result.demand.mode == "Y" and not result.demand.pitch_valid
    assert source.metrics()["selections"] == 1


def test_radio_callback_does_not_mutate_console_from_another_thread(fly, local_distance):
    source, callbacks = local_distance
    epoch = fly["session"].yaw_preview.selection_epoch
    thread = threading.Thread(target=lambda: source.request_selection("1234abcd:1", "D"))
    thread.start()
    thread.join(timeout=1.)
    assert not thread.is_alive() and fly["session"].yaw_preview.selection_epoch == epoch
    callbacks.pop()()
    assert source.snapshot().selection_result.success


@pytest.mark.parametrize("change", ["delayed", "camera", "closed"])
def test_queued_distance_selection_cannot_capture_late_or_different_reference(fly, local_distance, change):
    source, callbacks = local_distance
    source.request_selection("1234abcd:1", "D")
    epoch = fly["session"].yaw_preview.selection_epoch
    if change == "delayed":
        fly["now"][0] += .2
    elif change == "camera":
        fly["session"].video_source_id = "another-camera"
    else:
        source.close()
    callbacks.pop()()
    assert fly["session"].yaw_preview.selection_epoch == epoch
    assert source.snapshot().error and source.snapshot().demand is None


def test_closed_owner_loop_withdraws_previous_pitch(fly, local_distance):
    source, callbacks = local_distance
    enable(source, callbacks)
    def stopped(callback):
        raise RuntimeError("loop closed")
    source.schedule = stopped
    source.request_selection("1234abcd:2", "D")
    assert source.snapshot().error and source.snapshot().demand is None
    source.publish()
    assert source.snapshot().error


def test_repeated_reads_and_stalled_owner_cannot_extend_image_authority(fly, local_distance):
    source, callbacks = local_distance
    selected = enable(source, callbacks)
    fly["now"][0] = 1.1
    source.publish()
    sample = source.snapshot()
    assert sample.demand.deadline == pytest.approx(selected.demand.deadline)
    assert sample.demand.pitch == 0
    fly["now"][0] = 2.
    assert source.snapshot() == sample  # No publication occurred during the stall.
    assert sample.demand.deadline < 102.
    source.publish()
    sample = source.snapshot()
    assert sample.error or not sample.demand.valid
    assert sample.error or not sample.demand.pitch_valid


def test_pitch_withdraws_on_clipped_box_while_yaw_can_continue(fly, local_distance):
    source, callbacks = local_distance
    selected = enable(source, callbacks)
    sample = observe(fly, source, 1.1, [person(top=.005)])
    assert not sample.error, source.metrics()["last_error"]
    assert sample.demand.valid and not sample.demand.pitch_valid
    assert sample.demand.pitch == 0
    assert sample.demand.reference_height == selected.demand.reference_height


def test_target_loss_stops_pitch_then_requires_distinct_strong_frames(fly, local_distance):
    source, callbacks = local_distance
    initial = enable(source, callbacks)
    lost = observe(fly, source, 1.1, [])
    assert not lost.demand.valid and not lost.demand.pitch_valid
    first = observe(fly, source, 1.2)
    assert not first.demand.pitch_valid
    fly["now"][0] += .01
    source.publish()
    assert not source.snapshot().error, source.metrics()["last_error"]
    assert not source.snapshot().demand.pitch_valid
    for at in (1.3, 1.4, 1.5):
        sample = observe(fly, source, at)
    assert sample.demand.valid and sample.demand.pitch_valid
    assert sample.demand.reference_height == initial.demand.reference_height


def test_analysis_frame_mismatch_cannot_supply_pitch(fly, local_distance):
    source, callbacks = local_distance
    enable(source, callbacks)
    frame = fly["vision"]._frame
    # Session preview still refers to the selected analyzed image; a separate
    # candidate with mismatched sequence cannot provide distance geometry.
    fly["vision"]._frame = replace(frame, sample=replace(frame.sample, sequence=9999))
    source.publish()
    sample = source.snapshot()
    assert sample.error or not sample.demand.pitch_valid


@pytest.mark.parametrize("token,mode", [("1234abcd:1", "N"), ("1234abcd:1", "bad"),
                                         ("1234abcd:2147483648", "D"), ("bad", "Y")])
def test_invalid_radio_transaction_never_schedules(token, mode, local_distance):
    source, callbacks = local_distance
    with pytest.raises(PreviewError):
        source.request_selection(token, mode)
    assert not callbacks


def test_service_binds_distance_mailbox_and_exposes_pitch_preview(fly):
    callbacks = []
    class Loop:
        call_soon_threadsafe = staticmethod(callbacks.append)
    service = DistanceAssistService("/dev/not-opened", 8080, clock=lambda: fly["now"][0] + 100.)
    try:
        fly["observe"](1., [person()])
        service.bind_console(fly["session"], Loop())
        assert isinstance(service._local_source, LocalDistanceSource)
        service.publish_source()
        sample = enable(service._local_source, callbacks)
        assert sample.demand.pitch_valid
        preview = service.snapshot()["distance_preview"]
        assert preview["experimental"] and preview["valid"]
        assert preview["reference_height"] == .5
    finally:
        service.close()
    assert not service.snapshot()["distance_preview"]["valid"]
