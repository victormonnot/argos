"""The designated target, from detections to the view the guidance law consumes.

**The defect this module exists to not repeat.** In the lab, whether the target was
still valid was decided inside the function that processed a frame:
``coasting = (now - last_found) < coast_t``, computed once, stored, and read later by
the control loop. So the answer only changed when a frame arrived. Stop the camera
and the stored answer stops changing with it: a bench with a hand-driven clock left
``has=True`` and ``found=True`` after 2.005 s of silence against an announced hold of
1.5 s, and the loop went on submitting a target 2.305 s old. Nothing was wrong with
the hold time. What was wrong is that a fact about *time* was being refreshed by
*frames*.

So here, **every derived answer is a function of the clock and is computed when it is
read.** :meth:`Tracker.view` takes ``now`` and can be called at any cadence,
including when no frame has arrived for a minute; the state it reads from is a set of
*instants*, never a set of stored booleans. A control loop running faster than its
camera -- which is the normal case, and the whole point of detecting sparsely and
tracking in between -- gets a truthful answer on every cycle.

**A stalled source is not the same failure as a lost target, and it is worse.**
Coasting is a bet that the target is still roughly where it was and that a detection
will come back in a moment. If frames have stopped, that bet cannot pay off: no
evidence can arrive to settle it. So a stalled source ends the lock rather than
letting it coast to the end of its window, and the two are reported apart, because an
operator whose camera died needs to be told that and not "target lost".

That is the general lesson from the same audit, and it is worth stating plainly:
**receiving commands regularly does not prove the observations behind them are
recent.** A timeout on commands does not detect a dead camera. This layer is where
that gets detected, and the safety layer's target-age limit is the independent second
check, not the first one.

**A designation ends once, and it stays ended.** Whichever door notices -- a read
that finds the coast has run out, or a frame arriving after the deadline -- the end is
recorded rather than merely reported. An expiry that was shown to the operator but
left the lock alive was a designation that a later frame could revive, and a result
computed from an image captured before the deadline could arrive after it and do
exactly that. Both doors now evaluate the same predicate against a clock reading, and
only :meth:`Tracker.designate` ever starts a designation.

**What this layer does not do.** It does not detect anything: it consumes
:class:`argos.core.Detection` from a :class:`argos.perception.Detector` and never
looks at a pixel. It runs no filter and estimates no motion -- the derivative that
damps the guidance law is computed by the law, from the image error, which is where
the measurement actually is. And it is not an identity: association here is proximity
in the image, which is a heuristic that two people crossing will defeat.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Sequence

from argos.core import Detection, TargetView
from argos.safety.validate import check_clock, check_detection

from .frame import Frame, check_frame


class TrackState(Enum):
    """What the tracker believes about its designation, as of the instant asked.

    Reported alongside the view because these are not interchangeable and the
    difference decides what an operator should be told. ``LOST`` means the target
    was not found for longer than the coast window; ``STALLED`` means nothing has
    been *looked at* for longer than the frame limit, which is a broken camera or a
    broken link and not a target that moved.
    """

    IDLE = "idle"            # nothing is designated
    TRACKING = "tracking"    # the latest frame processed carried a detection
    COASTING = "coasting"    # no detection lately, still inside the coast window
    LOST = "lost"            # the coast window ran out
    STALLED = "stalled"      # frames have stopped arriving
    NO_CLOCK = "no_clock"    # the clock reading itself cannot be used
    FUTURE = "future"        # the evidence is stamped ahead of the clock

    # There is no "designated but never looked at": a designation is taken from a
    # frame, so that state is not representable and is not listed. A state machine
    # with a member nothing can reach is one more thing a reader has to rule out.


class Stamp(Enum):
    """Which clock reading the age of a detection is measured from.

    Not decoration. With ``RECEIPT`` the transport delay is invisible, so every age
    this tracker reports is a **lower bound** and every latency quoted from it is
    smaller than the truth. A measurement that does not say which of these it used
    is not a measurement anybody can check.
    """

    NONE = "none"
    CAPTURE = "capture"      # the sensor said when it exposed the image
    RECEIPT = "receipt"      # only the arrival is known; ages are lower bounds


@dataclass(frozen=True)
class TrackPolicy:
    """How long to believe things, and how far to look. Policy, not measurement.

    None of these was derived from a measured property of a camera or a detector.
    They are choices, they are configurable, and they are tested as choices.
    """

    coast: float = 0.5
    """s, how long a designation survives with no detection.

    Coasting through a brief occlusion is a designed behaviour; coasting is also a
    guess, and :class:`argos.core.TargetView` expects a consumer to slow down rather
    than act on it confidently.
    """

    max_frame_age: float = 0.3
    """s, past which the source counts as stalled.

    Above the interval of any camera this is meant to run on, and below the coast
    window on purpose: a stall should be noticed before the coast expires, so the
    operator is told which of the two happened.
    """

    gate: float = 0.25
    """Normalised image distance within which a detection may be associated with the
    designation. Generous, because a detector flickers and a target reacquired one
    frame later has not moved far; not unlimited, because at some radius this stops
    being the same object."""

    min_confidence: float = 0.25
    """Below this a detection is not considered for association at all."""

    max_clock_skew: float = 0.0
    """s, how far ahead of the clock stored evidence may be stamped and still be used.

    Zero, matching :attr:`argos.safety.Envelope.max_clock_skew` and
    :attr:`argos.guidance.GuidanceGains.max_clock_skew`, and the three are meant to
    stay equal; a test pins them together. Everything on one world reads one clock and
    that clock only moves forward, so evidence stamped ahead of it did not come from a
    correct producer. It is not unusually fresh information: it is information whose
    instant cannot be placed on this timeline at all, and the arithmetic that used to
    absorb it turned "no fresh evidence" into "measured just now".
    """

    def __post_init__(self) -> None:
        for name in ("coast", "max_frame_age", "gate", "min_confidence"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"TrackPolicy.{name} must be finite and positive, got {value!r}"
                )
        if not math.isfinite(self.max_clock_skew) or self.max_clock_skew < 0.0:
            raise ValueError(
                f"TrackPolicy.max_clock_skew must be finite and not negative, got "
                f"{self.max_clock_skew!r}"
            )
        if self.min_confidence > 1.0:
            raise ValueError(
                f"TrackPolicy.min_confidence must be within 0..1, got "
                f"{self.min_confidence!r}"
            )


@dataclass(frozen=True)
class TrackTelemetry:
    """Why the view says what it says. The field to read first is ``state``."""

    state: TrackState = TrackState.IDLE

    stamp: Stamp = Stamp.NONE
    """Where the age of the **adopted detection** comes from.

    It describes the detection the designation is actually resting on, and it changes
    only when a new one is adopted. It used to be rewritten by every frame processed,
    so an empty frame that happened to carry a capture stamp relabelled an age still
    measured from a receipt -- presenting a lower bound as a measurement, which is the
    one thing this pair of names exists to prevent.
    """

    frame_stamp: Stamp = Stamp.NONE
    """Where the **last frame processed** could have been dated from. A property of
    the source rather than of the designation, kept apart from ``stamp`` for that
    reason."""

    frame_age: float = 0.0       # s since the last frame was received
    detection_age: float = 0.0   # s since the last real detection
    problem: str = ""            # why a frame, a detection or a reading was declined


class Tracker:
    """One designated target, followed across frames.

    Holds instants and observations. It holds no booleans about validity, because a
    stored boolean is a fact that was true once.

    **Reading is not free of consequence, deliberately.** :meth:`view` ends a
    designation whose time is up rather than reporting it as over and leaving it
    alive. The alternative was tried and is worse: a lock reported ``LOST`` to the
    operator while still armed inside the tracker is one a later frame can revive,
    and it did.
    """

    def __init__(self, policy: TrackPolicy | None = None) -> None:
        self.policy = policy or TrackPolicy()
        self.rejected_detections = 0
        """Detector outputs declined as unusable. A count that climbs is a detector
        that has changed convention or broken, and it is invisible otherwise."""

        self.rejected_frames = 0
        """Frames declined because their stamps could not be used or did not cohere.
        Counted apart from stale ones: a frame that arrives out of order is a source
        behaving normally under load, a frame stamped in the future is not."""

        self.stale_frames = 0
        """Frames declined because they were not newer than the one already
        processed. A source handing back its cache is normal and cheap to ignore;
        a source going backwards is not, and both land here."""

        # Source state. It is not part of any designation and does not go away with
        # one: a camera that has stopped is worth seeing whether or not anything is
        # designated, and the frame ordering check has to survive a lock ending.
        self._last_frame: Frame | None = None
        self._frame_stamp = Stamp.NONE

        self._forget(None)

    def release(self) -> None:
        """Drop the designation, at the operator's request. State returns to idle."""
        self._forget(None)

    def _end(self, reason: TrackState, age: float) -> None:
        """End the designation because time ran out, and remember which way.

        Separate from :meth:`release` only in what it records. An operator who let go
        needs no explanation; one whose designation ended by itself needs to know
        whether the target was lost or the camera stopped, and that answer must
        outlive the lock it describes, and so must how old the evidence was when it
        ran out -- that number is what a log needs and it cannot be recomputed once
        the instant it came from is gone. It is frozen there rather than left to grow:
        a growing age after the end would suggest something is still being followed.
        """
        self._forget(reason, age)

    def _forget(self, reason: TrackState | None, age: float = 0.0) -> None:
        self._ended = reason
        self._ended_age = age
        self._locked = False
        self._label: str | None = None
        self._cx = 0.5
        self._cy = 0.5
        self._error_x = 0.0
        self._error_y = 0.0
        self._size = 0.0
        self._confidence = 0.0
        self._t_detected: float | None = None
        self._stamp = Stamp.NONE
        self._detected_on_last_frame = False
        self.telemetry = TrackTelemetry(
            state=reason or TrackState.IDLE, frame_stamp=self._frame_stamp,
            detection_age=age,
        )

    @property
    def locked(self) -> bool:
        return self._locked

    # --- taking and keeping a designation ---------------------------------

    def designate(self, detection: Detection, frame: Frame) -> None:
        """Lock onto one detection from one frame.

        An operator act, and the only way a lock is ever taken: nothing here acquires
        a target on its own, and nothing recovers one that ended. Raises on an
        unusable detection or frame, because designating is a deliberate call from a
        console and a bad one is a programmer error rather than a runtime condition
        to absorb silently.
        """
        problem = check_detection(detection)
        if problem:
            raise ValueError(f"cannot designate an unusable detection: {problem}")
        problem = check_frame(frame)
        if problem:
            raise ValueError(f"cannot designate from an unusable frame: {problem}")

        self._forget(None)
        self._locked = True
        self._label = detection.label or None
        self._last_frame = frame
        instant, stamp = self._instant(frame)
        self._frame_stamp = stamp
        self._adopt(detection, instant, stamp)
        # Reported straight away. Telemetry is otherwise written by reads, and leaving
        # a fresh designation described as idle until somebody happens to call `view`
        # would misreport the one moment an operator is certainly watching.
        self.telemetry = TrackTelemetry(
            state=TrackState.TRACKING, stamp=stamp, frame_stamp=stamp
        )

    def update(self, frame: Frame, detections: Sequence[Detection], now: float) -> None:
        """Process one frame's detections against the designation.

        ``now`` is the world clock and is **not optional**. It used to be missing, and
        the frame's own receipt stamp stood in for it -- which made the value being
        validated its own reference. A single frame stamped an hour ahead then looked
        like an hour of silence: the designation was ended as ``STALLED`` with an age
        of nine hundred seconds, before anything had compared that stamp to the actual
        time, and no later valid frame could bring it back because the end is
        permanent by design. One bad stamp destroyed a healthy track.

        Every other door in this project is handed the clock rather than inferring one
        -- :meth:`view`, the guidance law, the gate -- and this one now matches them.

        Declines rather than raises: a detector and a camera are third parties and a
        broken output is an ordinary event, not a reason to stop a control loop. What
        is declined never reaches the stored centre -- an unusable number entering a
        memory is not one bad frame, it is every frame until the lock is dropped,
        which is the defect the guidance law had and the reason the same predicates
        are used here.
        """
        problem = check_clock(now)
        if problem:
            # No trusted reading, so nothing can be judged. Refusing to touch anything
            # is the only honest move, and the memory survives for the next call.
            self.rejected_frames += 1
            self.telemetry = replace(
                self.telemetry, problem=f"frame declined, clock unusable: {problem}"
            )
            return

        problem = check_frame(frame)
        if problem:
            self.rejected_frames += 1
            self.telemetry = replace(self.telemetry, problem=f"frame declined: {problem}")
            return

        if frame.t_received > now + self.policy.max_clock_skew:
            # Checked before it can act on anything, which is the whole point: this is
            # the stamp under suspicion, so it must not be the thing that decides
            # whether the designation has run out of time. Nothing is touched -- not
            # the designation, not the source state -- so the next correctly stamped
            # frame is processed normally. A source whose stamps run ahead delivers
            # nothing usable, and the real clock ends the designation on its own.
            self.rejected_frames += 1
            self.telemetry = replace(
                self.telemetry,
                problem=(
                    f"frame is stamped {frame.t_received - now:.3f} s ahead of the "
                    f"clock, tolerance {self.policy.max_clock_skew:.3f} s"
                ),
            )
            return

        if self._last_frame is not None and frame.seq <= self._last_frame.seq:
            # The same cached image handed back, or one arriving after a newer one.
            # Either way there is no new measurement, and treating it as one would
            # manufacture a detection instant that never happened.
            self.stale_frames += 1
            return

        instant, stamp = self._instant(frame)
        self._frame_stamp = stamp

        # Evaluated against the clock the caller supplied, never against a stamp that
        # travelled with the data. Two different ways of getting this wrong have now
        # been paid for: judging the old designation by the *capture* instant let a
        # delayed result revive something that had already run out, and judging it by
        # the *receipt* stamp let one frame from the future fabricate a silence that
        # never happened.
        self._expire(now)

        if not self._locked:
            self._last_frame = frame
            return

        usable = []
        for detection in detections:
            problem = check_detection(detection)
            if problem:
                self.rejected_detections += 1
                continue
            if detection.confidence < self.policy.min_confidence:
                continue
            if self._label is not None and detection.label != self._label:
                continue
            usable.append(detection)

        best, best_distance = None, math.inf
        for detection in usable:
            distance = math.hypot(detection.cx - self._cx, detection.cy - self._cy)
            if distance < best_distance:
                best, best_distance = detection, distance

        self._last_frame = frame
        if best is not None and best_distance <= self.policy.gate:
            self._adopt(best, instant, stamp)
        else:
            # No detection this frame. The stored error and size are **frozen**, not
            # zeroed: a zero error would read as "centred", which is false, and it
            # would make the designation lie exactly when it is becoming fragile. The
            # provenance of the age is frozen with them, because the age still
            # describes the detection this frame did not replace.
            self._detected_on_last_frame = False

    # --- what a consumer reads --------------------------------------------

    def view(self, now: float) -> TargetView:
        """The designation as of ``now``, computed now.

        Safe to call at any cadence and with no frame in between: that is the whole
        point. Every boolean below is derived from an instant and this clock reading,
        so a source that has gone quiet expires the target by the passage of time
        rather than waiting for an image that is not coming.
        """
        problem = check_clock(now)
        if problem:
            # Nothing can be compared against an unreadable clock, and answering
            # "still tracking" because every comparison against NaN is false would be
            # the failure this module is about, arriving through the other door. The
            # memory is left exactly as it was, so a readable clock recovers it.
            self.telemetry = replace(
                self.telemetry, state=TrackState.NO_CLOCK,
                problem=f"clock unusable: {problem}",
            )
            return TargetView(has=False, found=False, t=None)

        frame_age = (
            0.0 if self._last_frame is None else now - self._last_frame.t_received
        )

        if not self._locked or self._t_detected is None:
            # Whatever ended the designation is still what a reader needs to be told,
            # and it is reported until a new one is taken.
            self.telemetry = TrackTelemetry(
                state=self._ended or TrackState.IDLE,
                stamp=self._stamp, frame_stamp=self._frame_stamp,
                frame_age=max(0.0, frame_age), detection_age=self._ended_age,
            )
            return TargetView(has=False, found=False, age=self._ended_age, t=now)

        ahead = max(self._t_detected - now, -frame_age)
        if ahead > self.policy.max_clock_skew:
            # Evidence stamped ahead of the clock is a second time base, not a very
            # fresh measurement. Refused here, before any age is computed: the
            # subtraction gives a negative number and flooring it at zero used to
            # report a designation with no fresh evidence as if it had just been
            # measured. Nothing is ended, because which of the two readings is wrong
            # is not knowable from here, and a readable pair recovers.
            self.telemetry = replace(
                self.telemetry, state=TrackState.FUTURE,
                stamp=self._stamp, frame_stamp=self._frame_stamp,
                problem=(
                    f"evidence is stamped {ahead:.3f} s ahead of the clock, "
                    f"tolerance {self.policy.max_clock_skew:.3f} s"
                ),
            )
            return TargetView(has=False, found=False, t=now)

        # Floored only to absorb a tolerated skew; an incoherent stamp was refused
        # above rather than flattened into zero here. Taken before the expiry below,
        # because that is the age the designation had when it ran out and it is the
        # number a log needs.
        detection_age = max(0.0, now - self._t_detected)
        frame_age = max(0.0, frame_age)

        # Reading is what notices, so reading is what ends it. See the class note.
        self._expire(now)
        if not self._locked:
            self.telemetry = TrackTelemetry(
                state=self._ended or TrackState.IDLE,
                stamp=self._stamp, frame_stamp=self._frame_stamp,
                frame_age=frame_age, detection_age=detection_age,
            )
            return TargetView(has=False, found=False, age=detection_age, t=now)

        state = TrackState.TRACKING if self._detected_on_last_frame else TrackState.COASTING
        self.telemetry = TrackTelemetry(
            state=state, stamp=self._stamp, frame_stamp=self._frame_stamp,
            frame_age=frame_age, detection_age=detection_age,
        )

        return TargetView(
            has=True,
            found=self._detected_on_last_frame,
            error_x=self._error_x,
            error_y=self._error_y,
            size=self._size,
            age=detection_age,
            confidence=self._confidence,
            t=now,
        )

    # --- internals ---------------------------------------------------------

    def _expire(self, now: float) -> None:
        """End the designation if this clock reading says its time is up.

        The single predicate both doors use, so they cannot disagree about whether a
        designation is over. Idempotent, and it never revives anything.

        The gap in the *source* is tested first. When both are true -- frames stopped
        long enough that the coast ran out during the silence -- the stall is the
        cause and the lost target is its consequence, and reporting the consequence
        would send an operator looking for a person instead of a camera.
        """
        if not self._locked or self._t_detected is None:
            return
        age = max(0.0, now - self._t_detected)
        if (self._last_frame is not None
                and now - self._last_frame.t_received > self.policy.max_frame_age):
            self._end(TrackState.STALLED, age)
        elif age > self.policy.coast:
            self._end(TrackState.LOST, age)

    @staticmethod
    def _instant(frame: Frame) -> tuple[float, Stamp]:
        """When a detection in this frame happened, and which stamp says so.

        Capture when the chain carries it. Otherwise receipt, which is *later* than
        the truth and therefore makes every age look younger than it is -- returned
        alongside so the flattering direction is never quoted as measured. Pure: the
        answer describes this frame, and it is stored only if a detection from this
        frame is actually adopted.
        """
        if frame.t_capture is not None:
            return frame.t_capture, Stamp.CAPTURE
        return frame.t_received, Stamp.RECEIPT

    def _adopt(self, detection: Detection, instant: float, stamp: Stamp) -> None:
        """Take this detection as the designation's latest evidence.

        The instant and its provenance are stored together, and only here. They
        describe the detection the designation rests on, so nothing that fails to
        replace that detection may change either of them.
        """
        self._cx, self._cy = detection.cx, detection.cy
        # Normalised to -1..1 about the image centre: +x is right, +y is down, the
        # image convention. The law reads error_x; a consumer pointing a gimbal
        # reads error_y, which is why it is produced even though the law ignores it.
        self._error_x = (detection.cx - 0.5) * 2.0
        self._error_y = (detection.cy - 0.5) * 2.0
        # The only range information on this aircraft: box height over image height.
        # See argos.core.TargetView for the ceiling this puts on any size threshold.
        self._size = detection.h
        self._confidence = detection.confidence
        self._t_detected = instant
        self._stamp = stamp
        self._detected_on_last_frame = True
