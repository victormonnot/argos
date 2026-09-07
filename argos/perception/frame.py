"""Images, and the one fact about an image that is easy to get wrong: when it happened.

**A frame is stamped by its source, never by its reader.** The lab took the
timestamp *after* pulling the latest image out of a cache, and called the result the
instant of capture. It is not: it is the instant the reader got round to looking. A
latency computed from it silently excludes everything before the read -- the sensor's
own exposure, the transport, and however long the image sat in the cache waiting to
be noticed. The number that comes out is smaller than the truth, which is the
direction that flatters, and nothing about it says so.

So this module splits the question in two and refuses to let the halves be confused:

    t_received   when this process first held the pixels.  ALWAYS known, because the
                 source stamps it the moment it takes delivery.
    t_capture    when the sensor actually exposed the image.  Known only when the
                 chain carries it -- a simulator's message header does, an analog
                 downlink through a capture stick does not.

``t_capture`` stays ``None`` when it is not known. It is **never** filled in from
``t_received``: that substitution would turn "we cannot see the transport delay" into
"the transport delay is zero", and the whole point of measuring is to find out which.

The two are read through two differently named methods, :meth:`Frame.age` and
:meth:`Frame.latency`, so that a figure cannot be quoted as the other by accident.
Anything publishing a latency has to say which stamp it came from.

**A source publishes an array it will never touch again.** The image is marked
read-only at publication rather than copied on every read: a 30 Hz source copying a
full frame per consumer is a real cost, and the rule that removes the need for it is
simply that a published frame is finished. Marking it enforces the rule at the
boundary where it is easy to break; it is not a sandbox, and an array that is a view
into a buffer somebody else keeps writing can still change underneath.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from argos.safety.validate import check_scalar


@dataclass(frozen=True)
class Frame:
    """One image, with the provenance a consumer needs to know what it is holding."""

    image: np.ndarray
    """Pixels. Read-only from publication onward; a consumer that draws takes a copy."""

    t_received: float
    """When the source took delivery, on the world clock (:meth:`argos.core.World.time`).

    Stamped by the source, in the callback or read that first held the bytes. A
    consumer must not compute this itself: by the time a consumer can, the answer has
    already moved.
    """

    seq: int
    """Strictly increasing per source. Two reads returning the same ``seq`` returned
    the same image, which is how a control loop faster than its camera avoids running
    a detector repeatedly over one frame -- and how a frame counter stops counting
    loop iterations."""

    t_capture: float | None = None
    """When the sensor exposed the image, on the same clock, or ``None`` if unknown.

    ``None`` is a real answer and has to be handled as one. It means the age of
    anything derived from this frame is a **lower bound**: the unmeasured part of the
    chain sits before ``t_received`` and is invisible from here.
    """

    def __post_init__(self) -> None:
        image = self.image
        if isinstance(image, np.ndarray) and image.flags.writeable:
            # Publication is the handover. Doing it here rather than trusting every
            # source to remember is the difference between a rule and a convention.
            image.flags.writeable = False

    def age(self, now: float) -> float:
        """Seconds since this frame was **received**. Always available."""
        return now - self.t_received

    def latency(self, now: float) -> float | None:
        """Seconds since this frame was **captured**, or ``None`` if the source cannot say.

        Deliberately not falling back on :attr:`t_received`. A caller that needs a
        number regardless can use :meth:`age` and must then say that is what it
        measured.
        """
        return None if self.t_capture is None else now - self.t_capture


@runtime_checkable
class FrameSource(Protocol):
    """Where images come from. The interface that changes between topologies.

    A ground station reading an analog downlink through a capture stick and a
    companion computer reading a CSI sensor are two implementations of this and
    nothing else: between here and the command going out, the code is the same. That
    is the property the two-topology plan rests on, and it only holds as long as
    nothing downstream names a particular source.
    """

    def read(self) -> Frame | None:
        """The most recent frame, or ``None`` if none has ever arrived.

        Returns the same frame again when nothing new has come in -- it does not
        block and does not wait. Consumers compare :attr:`Frame.seq` to decide
        whether there is anything to do.
        """
        ...

    def close(self) -> None:
        """Release the device or subscription. Idempotent."""
        ...


def check_frame(frame: Frame) -> str:
    """Empty if this frame's stamps are usable and coherent, otherwise why they are not.

    Checked at the doors that take frames, before anything derived from them is
    stored, and never left to be discovered downstream. An unusable capture stamp is
    not a small inaccuracy: an age computed from ``NaN`` compares false against every
    limit, and one computed from a stamp in the future comes out negative and then
    gets floored to zero, so a designation with no fresh evidence reads as if it had
    just been measured. Both used to happen here, and both looked like a perfectly
    healthy target.

    ``t_capture`` after ``t_received`` is refused rather than tolerated. The two are
    on one clock (:meth:`argos.core.World.time`), an image cannot arrive before it was
    taken, and a stamp saying otherwise means a second time base leaked in -- the same
    reasoning, and the same zero tolerance, as :attr:`argos.safety.Envelope.max_clock_skew`.

    ``None`` for ``t_capture`` stays legitimate throughout: it means the chain cannot
    say, which is a real answer and the reason the field is optional.
    """
    why = check_scalar(frame.t_received, "frame receipt time")
    if why:
        return why

    if isinstance(frame.seq, bool) or not isinstance(frame.seq, int):
        return f"frame seq is not an integer: {type(frame.seq).__name__}"

    if frame.t_capture is not None:
        why = check_scalar(frame.t_capture, "frame capture time")
        if why:
            return why
        if frame.t_capture > frame.t_received:
            return (
                f"frame was captured {frame.t_capture - frame.t_received:.3f} s after "
                f"it was received"
            )
    return ""
