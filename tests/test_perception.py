"""Perception, tested where it used to be wrong: against the clock.

The lab's tracker decided whether the target was still valid inside the function that
processed a frame, so the answer stopped changing when frames stopped arriving. Most
of what follows drives the clock forward with no frames at all, which is precisely
the case the old arrangement could not answer and the new one has to.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from argos.core import Detection, TargetView
from argos.perception import (
    Frame,
    check_frame,
    FrameSource,
    Stamp,
    TrackPolicy,
    TrackState,
    Tracker,
)
from argos.safety import check_target

IMAGE = np.zeros((48, 64, 3), dtype=np.uint8)


def frame(t: float, seq: int, t_capture: float | None = None) -> Frame:
    return Frame(image=IMAGE.copy(), t_received=t, seq=seq, t_capture=t_capture)


def person(cx: float = 0.5, cy: float = 0.5, h: float = 0.2, conf: float = 0.9) -> Detection:
    return Detection(cx=cx, cy=cy, w=h / 2, h=h, confidence=conf, label="person")


class CannedSource:
    """A frame source a test drives by hand, standing in for a camera.

    It is a source and not a camera: no sensor, no transport, no jitter, no dropped
    packets. What it exercises is the interface and the consumer's behaviour when the
    interface goes quiet.
    """

    def __init__(self) -> None:
        self._frame: Frame | None = None
        self._seq = 0
        self.closed = False

    def publish(self, t: float, t_capture: float | None = None) -> Frame:
        self._seq += 1
        self._frame = frame(t, self._seq, t_capture)
        return self._frame

    def read(self) -> Frame | None:
        return self._frame

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------
# The frame contract: who stamps an image, and what the stamp means
# --------------------------------------------------------------------------


def test_a_canned_source_is_a_frame_source() -> None:
    assert isinstance(CannedSource(), FrameSource)


def test_a_published_frame_cannot_be_written_through() -> None:
    """Publication is a handover: the source has finished with these pixels.

    Cheaper than copying on every read, which is what a 30 Hz source with more than
    one consumer would otherwise pay, and the rule is enforced at the boundary rather
    than left to every source to remember.
    """
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    published = Frame(image=image, t_received=1.0, seq=1)

    assert not published.image.flags.writeable
    with pytest.raises(ValueError):
        published.image[0, 0, 0] = 255


def test_a_source_that_hands_back_its_cache_is_visible_as_the_same_frame() -> None:
    """Two reads, one image: the sequence number is what says so.

    This is how a loop faster than its camera avoids running a detector repeatedly
    over one frame, and how a frame counter counts frames instead of loop iterations.
    """
    source = CannedSource()
    source.publish(t=100.0)

    assert source.read().seq == source.read().seq
    source.publish(t=100.1)
    assert source.read().seq == 2


def test_a_frame_reports_its_age_from_receipt_and_always_can() -> None:
    """Receipt is stamped by the source and is therefore never missing."""
    assert frame(t=100.0, seq=1).age(100.25) == pytest.approx(0.25)


def test_a_frame_with_no_capture_stamp_says_so_instead_of_guessing() -> None:
    """``None`` is the answer, not a gap to be filled from the arrival time.

    Substituting receipt would turn "the transport delay is invisible from here" into
    "the transport delay is zero", which is the flattering reading and the false one.
    An analog downlink through a capture stick genuinely cannot answer this.
    """
    blind = frame(t=100.0, seq=1)

    assert blind.latency(100.25) is None
    assert blind.age(100.25) == pytest.approx(0.25)


def test_a_frame_carrying_a_capture_stamp_measures_from_capture() -> None:
    """And the two differ by exactly the part the other measurement omits."""
    timed = frame(t=100.0, seq=1, t_capture=99.94)

    assert timed.latency(100.25) == pytest.approx(0.31)
    assert timed.age(100.25) == pytest.approx(0.25)
    assert timed.latency(100.25) > timed.age(100.25)


# --------------------------------------------------------------------------
# The defect this module exists to not repeat
# --------------------------------------------------------------------------


def test_a_silent_camera_expires_the_target_by_the_clock() -> None:
    """The audit's bench, with its numbers.

    The lab left ``has=True`` and ``found=True`` after 2.005 s of silence against an
    announced hold of 1.5 s, because the hold was recomputed only when a frame
    arrived. Here the same silence expires the designation, and it does so without
    any frame ever arriving to trigger it.
    """
    tracker = Tracker(TrackPolicy(coast=1.5, max_frame_age=0.3))
    tracker.designate(person(), frame(t=100.0, seq=1))
    assert tracker.view(100.0).has

    silent = tracker.view(102.005)

    assert not silent.has
    assert not silent.found
    assert tracker.telemetry.state is TrackState.STALLED
    assert silent.age == pytest.approx(2.005)


def test_the_answer_changes_with_the_clock_and_not_with_frames() -> None:
    """Called at any cadence, with nothing arriving in between, and still truthful."""
    tracker = Tracker(TrackPolicy(coast=1.5, max_frame_age=0.3))
    tracker.designate(person(), frame(t=100.0, seq=1))

    states = [
        (now, tracker.view(now).has, tracker.telemetry.state)
        for now in (100.0, 100.1, 100.2, 100.29, 100.31, 101.0)
    ]

    assert [has for _, has, _ in states] == [True, True, True, True, False, False]
    assert states[-1][2] is TrackState.STALLED


def test_a_stalled_source_is_reported_apart_from_a_target_that_was_lost() -> None:
    """Two different failures, two different things to tell an operator.

    A camera that died and a person who walked behind a wall both end the lock. Only
    one of them means the aircraft has stopped seeing anything at all.
    """
    policy = TrackPolicy(coast=0.4, max_frame_age=0.3)

    stalled = Tracker(policy)
    stalled.designate(person(), frame(t=100.0, seq=1))
    stalled.view(100.5)
    assert stalled.telemetry.state is TrackState.STALLED

    # Frames keep coming; the target is simply not in them.
    occluded = Tracker(policy)
    occluded.designate(person(), frame(t=100.0, seq=1))
    for i, t in enumerate((100.1, 100.2, 100.3, 100.4, 100.5), start=2):
        occluded.update(frame(t=t, seq=i), [], now=t)
        occluded.view(t)
    assert occluded.telemetry.state is TrackState.LOST


def test_a_stall_is_noticed_before_the_coast_window_runs_out() -> None:
    """Deliberate ordering: the operator learns *why* before the lock simply ends.

    Coasting is a bet that a detection will come back in a moment. With the source
    stopped that bet cannot pay off, so the lock ends at the stall rather than
    running out its window on evidence that cannot arrive.
    """
    tracker = Tracker(TrackPolicy(coast=1.5, max_frame_age=0.3))
    tracker.designate(person(), frame(t=100.0, seq=1))

    assert not tracker.view(100.4).has          # well inside the 1.5 s coast window
    assert tracker.telemetry.state is TrackState.STALLED
    assert tracker.telemetry.detection_age < 1.5


def test_coasting_holds_the_last_error_instead_of_centring_it() -> None:
    """A zero error would read as "centred", which is false exactly when it matters.

    The designation becomes fragile during a coast; reporting it as perfectly centred
    would make it lie at that moment rather than degrade honestly.
    """
    tracker = Tracker(TrackPolicy(coast=0.5, max_frame_age=0.3))
    tracker.designate(person(cx=0.8), frame(t=100.0, seq=1))
    committed = tracker.view(100.0).error_x
    assert committed == pytest.approx(0.6)

    tracker.update(frame(t=100.1, seq=2), [], now=100.1)   # a frame, and nothing in it
    coasting = tracker.view(100.1)

    assert tracker.telemetry.state is TrackState.COASTING
    assert coasting.has and not coasting.found
    assert coasting.error_x == pytest.approx(committed)


def test_a_view_without_a_designation_makes_no_claim_about_centring() -> None:
    """Once ``has`` is false the observables are zero, and that is not a measurement.

    A consumer must not act on such a view at all; reporting the last known error
    beside ``has=False`` would invite exactly that. The age survives, because how old
    the designation was when it ended is the useful thing in a log.
    """
    tracker = Tracker(TrackPolicy(coast=0.5, max_frame_age=0.3))
    tracker.designate(person(cx=0.9), frame(t=100.0, seq=1))

    dropped = tracker.view(101.0)

    assert not dropped.has
    assert dropped.error_x == 0.0
    assert dropped.size == 0.0
    assert dropped.age == pytest.approx(1.0)


# --------------------------------------------------------------------------
# What reaches the lock, and what never does
# --------------------------------------------------------------------------


def test_a_designation_that_ran_out_is_not_revived_by_the_next_frame() -> None:
    """A deliberate departure from the ported code, which re-acquired.

    Re-acquiring after a long gap means flying at whatever now sits nearest the last
    known position of a target that was lost, while reporting that the original
    designation is still being tracked. Taking a lock is an operator act; getting one
    back has to be one too.
    """
    tracker = Tracker(TrackPolicy(coast=0.4, max_frame_age=0.3))
    tracker.designate(person(cx=0.5), frame(t=100.0, seq=1))

    tracker.update(frame(t=105.0, seq=2), [person(cx=0.5)], now=105.0)   # five seconds later

    assert not tracker.locked
    assert not tracker.view(105.0).has
    assert tracker.telemetry.state is TrackState.STALLED      # and it still says why

    # The same refusal when the source never stopped and the target simply went.
    lost = Tracker(TrackPolicy(coast=0.4, max_frame_age=0.3))
    lost.designate(person(cx=0.5), frame(t=100.0, seq=1))
    for i, t in enumerate((100.2, 100.4, 100.6), start=2):
        lost.update(frame(t=t, seq=i), [], now=t)
    lost.update(frame(t=100.8, seq=5), [person(cx=0.5)], now=100.8)      # somebody reappears

    assert not lost.locked
    assert lost.telemetry.state is TrackState.LOST


def test_an_unusable_detection_never_reaches_the_lock() -> None:
    """One NaN in a memory is not one bad frame, it is every frame after it.

    The same lesson as the guidance law's, checked with the same predicate so that
    "usable" means one thing in the project. A bad frame costs a frame, not the
    designation.
    """
    tracker = Tracker(TrackPolicy(coast=1.0, max_frame_age=1.0))
    tracker.designate(person(cx=0.6), frame(t=100.0, seq=1))

    tracker.update(frame(t=100.1, seq=2), [Detection(math.nan, 0.5, 0.1, 0.2, 0.9, "person")], now=100.1)

    assert tracker.rejected_detections == 1
    assert tracker.view(100.1).error_x == pytest.approx(0.2)   # unchanged
    assert not check_target(tracker.view(100.1))

    tracker.update(frame(t=100.2, seq=3), [person(cx=0.7)], now=100.2)    # and it recovers
    recovered = tracker.view(100.2)
    assert recovered.found
    assert recovered.error_x == pytest.approx(0.4)


def test_a_frame_handed_over_twice_is_not_a_second_measurement() -> None:
    """A cached image read again carries no new evidence.

    Accepting it would move the detection instant forward without anything having
    been detected, which is how a stale designation starts looking fresh.
    """
    tracker = Tracker(TrackPolicy(coast=1.0, max_frame_age=1.0))
    tracker.designate(person(), frame(t=100.0, seq=1))

    repeat = frame(t=100.5, seq=1)                 # same seq, later arrival
    tracker.update(repeat, [person()], now=100.5)

    assert tracker.stale_frames == 1
    assert tracker.view(100.5).age == pytest.approx(0.5)


def test_a_frame_arriving_after_a_newer_one_is_ignored() -> None:
    """Out of order is not new either, and it must not move the reference backwards."""
    tracker = Tracker(TrackPolicy(coast=1.0, max_frame_age=1.0))
    tracker.designate(person(), frame(t=100.0, seq=5))

    tracker.update(frame(t=100.1, seq=3), [person(cx=0.9)], now=100.1)

    assert tracker.stale_frames == 1
    assert tracker.view(100.1).error_x == pytest.approx(0.0)


def test_an_unreadable_clock_never_reports_a_valid_target() -> None:
    """Every comparison against NaN is false, which would answer "still tracking".

    The same door the guidance law had to close. Checked first, so the classification
    below it is never reached with a clock that cannot order anything.
    """
    tracker = Tracker()
    tracker.designate(person(), frame(t=100.0, seq=1))

    blind = tracker.view(math.nan)

    assert not blind.has
    assert blind.t is None
    assert "clock" in tracker.telemetry.problem


def test_the_stamp_says_whether_an_age_is_a_lower_bound() -> None:
    """With receipt only, the transport delay is invisible and ages are lower bounds.

    Recorded rather than assumed, because a latency figure whose provenance is not
    stated is one nobody can check.
    """
    blind = Tracker()
    blind.designate(person(), frame(t=100.0, seq=1))
    blind.view(100.0)
    assert blind.telemetry.stamp is Stamp.RECEIPT

    timed = Tracker()
    timed.designate(person(), frame(t=100.0, seq=1, t_capture=99.9))
    assert timed.view(100.0).age == pytest.approx(0.1)      # the delay is counted
    assert timed.telemetry.stamp is Stamp.CAPTURE


def test_association_is_bounded_by_distance_label_and_confidence() -> None:
    """A heuristic, and bounded on three axes rather than trusted on none.

    None of this is identity: two people crossing will defeat proximity, and this
    layer does not pretend otherwise.
    """
    policy = TrackPolicy(coast=1.0, max_frame_age=1.0, gate=0.25, min_confidence=0.4)

    far = Tracker(policy)
    far.designate(person(cx=0.2), frame(t=100.0, seq=1))
    far.update(frame(t=100.1, seq=2), [person(cx=0.9)], now=100.1)
    assert not far.view(100.1).found

    other = Tracker(policy)
    other.designate(person(cx=0.5), frame(t=100.0, seq=1))
    other.update(frame(t=100.1, seq=2), [Detection(0.5, 0.5, 0.1, 0.2, 0.9, "car")], now=100.1)
    assert not other.view(100.1).found

    unsure = Tracker(policy)
    unsure.designate(person(cx=0.5), frame(t=100.0, seq=1))
    unsure.update(frame(t=100.1, seq=2), [person(cx=0.5, conf=0.3)], now=100.1)
    assert not unsure.view(100.1).found

    near = Tracker(policy)
    near.designate(person(cx=0.5), frame(t=100.0, seq=1))
    near.update(frame(t=100.1, seq=2), [person(cx=0.6)], now=100.1)
    assert near.view(100.1).found


def test_designating_an_unusable_detection_raises() -> None:
    """A console call, so a bad one is a programmer error and says so at the call."""
    tracker = Tracker()
    with pytest.raises(ValueError):
        tracker.designate(Detection(1.4, 0.5, 0.1, 0.2, 0.9), frame(t=100.0, seq=1))
    assert not tracker.locked


def test_releasing_drops_everything_derived_from_the_designation() -> None:
    tracker = Tracker()
    tracker.designate(person(cx=0.8), frame(t=100.0, seq=1))
    tracker.release()

    assert not tracker.locked
    assert tracker.view(100.0) == TargetView(has=False, found=False, t=100.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"coast": 0.0},
        {"coast": math.nan},
        {"max_frame_age": -1.0},
        {"gate": math.inf},
        {"min_confidence": 0.0},
        {"min_confidence": 1.5},
    ],
    ids=str,
)
def test_a_policy_that_describes_nothing_is_refused_at_construction(kwargs) -> None:
    with pytest.raises(ValueError):
        TrackPolicy(**kwargs)


def test_every_view_this_tracker_produces_is_one_the_safety_layer_can_read() -> None:
    """The layers meet here, so the contract between them is asserted here.

    A view the gate would refuse as malformed is a perception bug wearing a safety
    refusal, and the two are worth telling apart before a flight rather than after.
    """
    tracker = Tracker(TrackPolicy(coast=0.4, max_frame_age=0.3))
    tracker.designate(person(cx=0.7), frame(t=100.0, seq=1))

    for i, t in enumerate((100.1, 100.2, 100.6, 101.5), start=2):
        tracker.update(frame(t=t, seq=i), [person(cx=0.7)] if i % 2 else [], now=t)
        assert check_target(tracker.view(t)) == ""
    assert check_target(tracker.view(105.0)) == ""


def test_the_coast_window_can_run_out_between_two_frames() -> None:
    """The normal case at flight cadence, and the one a frame-driven tracker misses.

    A 64 Hz control loop reading a 10 Hz camera spends most of its cycles between
    frames. If the coast window ends there, the loop has to be told *there* -- waiting
    for the next frame to notice would mean flying on an expired designation for up to
    a frame interval, which is exactly the shape of the defect this module replaces.

    The source is healthy throughout: the last frame is well inside the stall limit,
    so what ends the lock is the coast and nothing else.
    """
    tracker = Tracker(TrackPolicy(coast=0.4, max_frame_age=0.3))
    tracker.designate(person(cx=0.8), frame(t=100.0, seq=1))
    tracker.update(frame(t=100.2, seq=2), [], now=100.2)          # a frame arrives, empty

    still_coasting = tracker.view(100.3)
    assert still_coasting.has and not still_coasting.found
    assert tracker.telemetry.state is TrackState.COASTING

    ran_out = tracker.view(100.45)                     # no new frame in between

    assert not ran_out.has
    assert tracker.telemetry.state is TrackState.LOST
    assert tracker.telemetry.frame_age <= 0.3          # the camera is fine; the lock is not


# --------------------------------------------------------------------------
# Timestamps: what a frame may say, and what a reading may conclude from it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "t_capture, reason",
    [
        (math.nan, "finite"),
        (math.inf, "finite"),
        ("bad", "real number"),
        (100.5, "after it was received"),
    ],
    ids=["nan", "inf", "string", "after-receipt"],
)
def test_a_frame_whose_capture_stamp_cannot_be_used_is_named_as_such(t_capture, reason) -> None:
    """Checked as a frame, before anything derived from it exists.

    A capture stamp after the receipt is refused rather than tolerated: the two are on
    one clock, an image cannot arrive before it was taken, and a stamp saying otherwise
    means a second time base leaked in.
    """
    assert reason in check_frame(frame(t=100.0, seq=1, t_capture=t_capture))


def test_a_frame_with_no_capture_stamp_is_perfectly_usable() -> None:
    """``None`` is a real answer and must not be confused with a broken one."""
    assert check_frame(frame(t=100.0, seq=1)) == ""


@pytest.mark.parametrize(
    "t_capture", [math.nan, math.inf, "bad", 100.5], ids=["nan", "inf", "string", "after-receipt"]
)
def test_designating_from_an_unusable_frame_raises(t_capture) -> None:
    """The operator's door raises, like the detection check beside it.

    Left unchecked, ``designate`` accepted a capture stamp of ``"bad"`` and the
    failure surfaced later as a ``TypeError`` inside a read -- an exception in a
    control loop, thrown by the layer that was supposed to refuse the input.
    """
    tracker = Tracker()
    with pytest.raises(ValueError):
        tracker.designate(person(), frame(t=100.0, seq=1, t_capture=t_capture))
    assert not tracker.locked


def test_an_unusable_capture_stamp_never_becomes_an_age_of_zero() -> None:
    """The arithmetic that used to absorb it, and what it produced.

    ``now - NaN`` is ``NaN``; ``max(0.0, NaN)`` is ``0.0``. So a designation resting on
    a frame with a broken capture stamp reported itself as measured this instant, and
    ``check_target`` downstream saw a perfectly well-formed view, for as long as the
    lock survived. The stamp is refused at the door instead, so the arithmetic is never
    reached with a value it cannot represent.
    """
    tracker = Tracker(TrackPolicy(coast=0.5))
    tracker.designate(person(cx=0.75), frame(t=100.0, seq=1))

    tracker.update(frame(t=100.125, seq=2, t_capture=math.nan), [person(cx=0.1)], now=100.125)

    assert tracker.rejected_frames == 1
    assert tracker.view(100.125).error_x == pytest.approx(0.5)   # the lock is untouched
    assert tracker.stale_frames == 0                             # and not miscounted

    # ... and the next usable frame is accepted normally: a refusal costs a frame.
    tracker.update(frame(t=100.25, seq=3, t_capture=100.2), [person(cx=0.8)], now=100.25)
    recovered = tracker.view(100.25)
    assert recovered.found
    assert recovered.error_x == pytest.approx(0.6)


def test_a_broken_capture_stamp_cannot_hold_a_designation_open() -> None:
    """The compound case: the age stopped growing, so the coast never ran out.

    Twenty empty frames over more than two seconds, against a coast of half a second.
    With the stamp refused, the same sequence ends the designation on time.
    """
    tracker = Tracker(TrackPolicy(coast=0.5))
    with pytest.raises(ValueError):
        tracker.designate(person(), frame(t=100.125, seq=1, t_capture=math.nan))

    tracker.designate(person(), frame(t=100.125, seq=1))
    for i in range(2, 22):
        tracker.update(frame(t=100.125 + 0.125 * i, seq=i), [], now=100.125 + 0.125 * i)

    ended = tracker.view(102.75)
    assert not ended.has
    assert tracker.telemetry.state is TrackState.LOST
    assert ended.age > 0.5


def test_evidence_stamped_ahead_of_the_clock_is_refused_rather_than_flattened() -> None:
    """A negative age floored to zero read as "measured just now".

    Refused before any age is computed, and the memory is left alone: which of the two
    readings is wrong is not knowable from here, so destroying a designation over it
    would be a guess. A coherent pair recovers immediately, which is the same treatment
    the guidance law gives an unreadable clock.
    """
    tracker = Tracker()
    tracker.designate(person(cx=0.75), frame(t=1000.0, seq=1))

    ahead = tracker.view(100.0)

    assert not ahead.has
    assert tracker.telemetry.state is TrackState.FUTURE
    assert "ahead of the clock" in tracker.telemetry.problem

    recovered = tracker.view(1000.0)
    assert recovered.has
    assert recovered.error_x == pytest.approx(0.5)


def test_an_unreadable_clock_and_a_future_stamp_are_told_apart() -> None:
    """Two different problems and two different things to look at."""
    tracker = Tracker()
    tracker.designate(person(), frame(t=100.0, seq=1))

    tracker.view(math.nan)
    assert tracker.telemetry.state is TrackState.NO_CLOCK

    tracker.view(99.0)
    assert tracker.telemetry.state is TrackState.FUTURE


# --------------------------------------------------------------------------
# A designation ends once, and stays ended
# --------------------------------------------------------------------------


def test_an_expiry_noticed_by_a_read_is_final() -> None:
    """Reporting an expiry without performing it left a lock a later frame could take.

    Every stamp below is coherent and increasing, and every capture precedes its own
    receipt: no clock is broken. What used to revive the designation was simply that
    the capture instant of the new frame fell before the deadline, while the aircraft
    had in fact been without information past it.
    """
    tracker = Tracker()                                   # default policy
    tracker.designate(person(), frame(t=100.125, seq=1, t_capture=100.0))
    tracker.update(frame(t=100.25, seq=2, t_capture=100.1875), [], now=100.25)
    tracker.update(frame(t=100.375, seq=3, t_capture=100.3125), [], now=100.375)

    ended = tracker.view(100.5625)
    assert not ended.has
    assert tracker.telemetry.state is TrackState.LOST
    assert not tracker.locked                             # and it is really over

    tracker.update(frame(t=100.625, seq=4, t_capture=100.4375), [person()], now=100.625)

    assert not tracker.locked
    assert not tracker.view(100.625).has
    assert tracker.telemetry.state is TrackState.LOST


def test_a_late_result_cannot_revive_a_designation_even_if_nobody_looked() -> None:
    """The same sequence with no read in between: ``update`` must reach the same answer.

    Both doors evaluate one predicate against a clock reading, so whether an expiry
    happened cannot depend on whether anybody asked.
    """
    tracker = Tracker()
    tracker.designate(person(), frame(t=100.125, seq=1, t_capture=100.0))
    tracker.update(frame(t=100.25, seq=2, t_capture=100.1875), [], now=100.25)
    tracker.update(frame(t=100.375, seq=3, t_capture=100.3125), [], now=100.375)
    tracker.update(frame(t=100.625, seq=4, t_capture=100.4375), [person()], now=100.625)

    assert not tracker.locked
    assert tracker.telemetry.state is TrackState.LOST


def test_a_result_captured_before_a_stall_does_not_undo_it() -> None:
    """A frame in flight when the camera stopped arrives after the stall was declared."""
    tracker = Tracker(TrackPolicy(coast=2.0, max_frame_age=0.3))
    tracker.designate(person(), frame(t=100.0, seq=1, t_capture=99.9))

    assert not tracker.view(100.5).has
    assert tracker.telemetry.state is TrackState.STALLED

    tracker.update(frame(t=100.6, seq=2, t_capture=100.05), [person()], now=100.6)

    assert not tracker.locked
    assert tracker.telemetry.state is TrackState.STALLED


def test_an_explicit_designation_is_what_starts_tracking_again() -> None:
    """The other half of the rule: refusing to revive must not mean refusing to work."""
    tracker = Tracker()
    tracker.designate(person(), frame(t=100.125, seq=1, t_capture=100.0))
    tracker.view(101.0)
    assert not tracker.locked

    tracker.designate(person(cx=0.6), frame(t=101.0, seq=2, t_capture=100.95))

    resumed = tracker.view(101.0)
    assert resumed.has and resumed.found
    assert tracker.telemetry.state is TrackState.TRACKING
    assert resumed.error_x == pytest.approx(0.2)


def test_how_old_the_designation_was_when_it_ended_survives_the_ending() -> None:
    """The number a log needs, and it cannot be recomputed once the instant is gone.

    Frozen rather than left to grow: an age still climbing after the end would suggest
    something is still being followed.
    """
    tracker = Tracker(TrackPolicy(coast=0.5, max_frame_age=5.0))
    tracker.designate(person(), frame(t=100.0, seq=1))

    assert tracker.view(100.75).age == pytest.approx(0.75)
    assert tracker.view(200.0).age == pytest.approx(0.75)   # ended then, not now


# --------------------------------------------------------------------------
# Where an age comes from, and what may change that answer
# --------------------------------------------------------------------------


def test_an_empty_frame_does_not_relabel_the_age_of_the_detection() -> None:
    """The provenance belongs to the adopted detection, not to the last frame seen.

    It used to be rewritten on every frame processed, before knowing whether anything
    would be adopted, so an empty frame that happened to carry a capture stamp
    presented a lower bound measured from a receipt as a measurement from capture --
    exactly the confusion the two names exist to prevent -- and the reverse in the
    other direction.
    """
    policy = TrackPolicy(coast=1.0, max_frame_age=1.0)

    from_receipt = Tracker(policy)
    from_receipt.designate(person(), frame(t=100.0, seq=1))          # no capture stamp
    from_receipt.update(frame(t=100.125, seq=2, t_capture=100.0625), [], now=100.125)
    assert from_receipt.view(100.25).age == pytest.approx(0.25)
    assert from_receipt.telemetry.stamp is Stamp.RECEIPT             # still a lower bound

    from_capture = Tracker(policy)
    from_capture.designate(person(), frame(t=100.0, seq=1, t_capture=99.9375))
    from_capture.update(frame(t=100.125, seq=2), [], now=100.125)                 # no capture stamp
    assert from_capture.view(100.25).age == pytest.approx(0.3125)
    assert from_capture.telemetry.stamp is Stamp.CAPTURE             # still measured


def test_a_real_detection_moves_the_instant_and_its_provenance_together() -> None:
    """Adoption is the only thing that changes either, and it changes both."""
    tracker = Tracker(TrackPolicy(coast=1.0, max_frame_age=1.0))
    tracker.designate(person(cx=0.5), frame(t=100.0, seq=1))
    assert tracker.telemetry.stamp is Stamp.RECEIPT

    tracker.update(frame(t=100.25, seq=2, t_capture=100.1875), [person(cx=0.5)], now=100.25)

    adopted = tracker.view(100.25)
    assert adopted.age == pytest.approx(0.0625)
    assert tracker.telemetry.stamp is Stamp.CAPTURE


def test_the_provenance_of_the_last_frame_is_reported_apart() -> None:
    """Useful, and a different fact: it describes the source, not the designation."""
    tracker = Tracker(TrackPolicy(coast=1.0, max_frame_age=1.0))
    tracker.designate(person(), frame(t=100.0, seq=1))
    tracker.update(frame(t=100.125, seq=2, t_capture=100.0625), [], now=100.125)

    tracker.view(100.25)
    assert tracker.telemetry.stamp is Stamp.RECEIPT       # the detection's
    assert tracker.telemetry.frame_stamp is Stamp.CAPTURE  # the source's


# --------------------------------------------------------------------------
# The clock a frame is judged against is never the frame's own stamp
# --------------------------------------------------------------------------


def test_the_update_door_is_handed_the_clock_and_will_not_guess_one() -> None:
    """No default, for the same reason ``engage`` has none in the guidance law.

    A default would have to be the frame's own receipt stamp, which is precisely the
    value under suspicion. Omitting the argument is a mistake worth failing at the
    call site rather than one that quietly reinstates the defect.
    """
    tracker = Tracker()
    tracker.designate(person(), frame(t=100.0, seq=1))
    with pytest.raises(TypeError):
        tracker.update(frame(t=100.1, seq=2), [person()])   # type: ignore[call-arg]


@pytest.mark.parametrize(
    "t_capture",
    [1000.0, None, 100.0625],
    ids=["future-capture-and-receipt", "future-receipt-no-capture", "future-receipt-past-capture"],
)
def test_a_frame_from_the_future_cannot_end_a_live_designation(t_capture) -> None:
    """One badly stamped frame used to destroy a healthy track, permanently.

    ``update`` took its notion of the current time from the frame it was supposed to
    be judging, so a frame stamped at 1000 while the clock read 100.125 looked like
    nine hundred seconds of silence: the designation was ended as ``STALLED`` with an
    age of 900 s, and because an ending is permanent -- which is what P2 required --
    no correctly stamped frame afterwards could bring it back.

    The refusal now happens before anything is touched, so the designation survives
    intact and the next valid frame is processed normally. All three variants matter:
    what is wrong is the receipt stamp, whatever the capture stamp does.
    """
    tracker = Tracker()
    tracker.designate(person(cx=0.75), frame(t=100.0, seq=1, t_capture=100.0))
    assert tracker.view(100.125).has

    tracker.update(frame(t=1000.0, seq=2, t_capture=t_capture), [person()], now=100.125)

    assert tracker.rejected_frames == 1
    assert tracker.locked, "a stamp from the future is not nine hundred seconds of silence"
    assert "ahead of the clock" in tracker.telemetry.problem
    still_tracking = tracker.view(100.125)
    assert still_tracking.has and still_tracking.found

    # ... and the next correctly stamped frame is taken exactly as if nothing happened.
    tracker.update(frame(t=100.25, seq=3, t_capture=100.25), [person(cx=0.8)], now=100.25)

    recovered = tracker.view(100.25)
    assert recovered.has and recovered.found
    assert recovered.age == pytest.approx(0.0)
    assert recovered.error_x == pytest.approx(0.6)


def test_a_refused_frame_does_not_become_the_source_state_either() -> None:
    """Refusing means touching nothing, including the record of the last frame seen.

    Storing it would move the stall reference into the future and make the source look
    perpetually healthy, which is the same defect wearing the opposite sign.
    """
    tracker = Tracker()
    tracker.designate(person(), frame(t=100.0, seq=1))

    tracker.update(frame(t=1000.0, seq=2), [person()], now=100.125)

    assert tracker.view(100.125).has
    assert tracker.telemetry.frame_age == pytest.approx(0.125)   # from the real frame


def test_a_clock_that_cannot_be_read_at_update_touches_nothing() -> None:
    """No trusted reading means nothing can be judged, so nothing is.

    The same treatment ``view`` gives an unreadable clock, and for the same reason: a
    designation must not be destroyed over a number that cannot be compared.
    """
    tracker = Tracker()
    tracker.designate(person(cx=0.75), frame(t=100.0, seq=1))

    tracker.update(frame(t=100.125, seq=2), [person(cx=0.1)], now=math.nan)

    assert tracker.rejected_frames == 1
    assert tracker.view(100.125).error_x == pytest.approx(0.5)
    assert tracker.view(100.125).has


def test_a_real_silence_still_ends_the_designation() -> None:
    """The control the fix must not weaken: a genuine gap expires, as P2 requires."""
    tracker = Tracker()
    tracker.designate(person(), frame(t=100.0, seq=1, t_capture=100.0))

    tracker.update(frame(t=101.0, seq=2, t_capture=101.0), [person()], now=101.0)

    assert not tracker.locked
    assert tracker.telemetry.state is TrackState.STALLED


def test_a_first_designation_from_the_future_still_recovers() -> None:
    """The designation door is left alone on purpose, and this is why it is safe.

    There is nothing to protect when a designation is being created: the frame *is*
    the memory. A designation taken from a frame the clock cannot place reads as
    ``FUTURE`` until the two agree, destroys nothing, and the next valid frame takes
    over. Refusing it at that door would break this without protecting anything.
    """
    tracker = Tracker()
    tracker.designate(person(), frame(t=1000.0, seq=1, t_capture=1000.0))
    assert not tracker.view(100.0).has
    assert tracker.telemetry.state is TrackState.FUTURE

    tracker.update(frame(t=100.125, seq=2, t_capture=100.125), [person()], now=100.125)

    resumed = tracker.view(100.125)
    assert resumed.has and resumed.found


def test_a_frame_processed_after_its_deadline_does_not_keep_the_designation_alive() -> None:
    """A backlog is not a time machine.

    With the future-stamp guard in place a frame's receipt is never *ahead* of the
    clock, but it can be well behind it: the loop stalls, and a frame that arrived
    within the coast window is finally processed after the window has closed. Judging
    the designation by that receipt would let a backlog hold a lock open past its
    deadline, one delayed frame at a time -- the same family as reviving an expired
    lock with a delayed capture, reached through the other stamp.

    The clock decides. The frame is data.
    """
    tracker = Tracker(TrackPolicy(coast=0.5, max_frame_age=5.0))
    tracker.designate(person(), frame(t=100.0, seq=1, t_capture=100.0))

    # Received at 100.4, inside the window; processed at 100.6, outside it.
    tracker.update(frame(t=100.4, seq=2, t_capture=100.4), [person()], now=100.6)

    assert not tracker.locked
    assert tracker.telemetry.state is TrackState.LOST
    assert not tracker.view(100.6).has
