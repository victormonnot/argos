"""Finite, passive reception capture, with cooperative stopping between batches.

The caller provides a new, unused link started at zero and owns its closure. The
binary output stream is also borrowed. One monotonic clock supplies elapsed run
time for polling and recording. No heartbeat, stream request or parameter is sent.

Normal duration expiry and a requested stop finalize the journal. A link or clock
failure leaves it incomplete and raises. Synchronous file writes are not a
real-time guarantee; the original receive journal does not capture discarded
bytes or raw transport buffering. Link byte diagnostics are returned separately.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import BinaryIO, Callable

from .link import MavlinkLink, _time
from .recording import RecordingWriter


class CaptureError(RuntimeError):
    """Capture could not reach a normal completion; the journal is incomplete."""


@dataclass(frozen=True)
class CaptureResult:
    ended_at: float
    recorded_events: int
    stop_reason: str
    rx_bytes: int
    bad_bytes: int


def capture_session(link: MavlinkLink, stream: BinaryIO, *, duration: float,
                    poll_interval: float = .01,
                    stop_requested: Callable[[], bool] = lambda: False,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> CaptureResult:
    """Capture for a finite duration; a stop callback is checked between batches.

    A CLI signal handler should set a flag, not interrupt a record write. All
    events returned by a poll are written before honoring that stop. The declared
    end is the actual elapsed time, which may exceed the requested duration if
    polling, writing or scheduling was slow. This is an observation utility.
    """
    duration, poll_interval = _time(duration), _time(poll_interval)
    if duration <= 0 or poll_interval <= 0:
        raise ValueError("duration and poll_interval must be positive")
    initial = link.report(0.)
    if (initial.closed or initial.rx_bytes or initial.traffic.tx
            or initial.traffic.tx_attempts or initial.invalid_sends):
        raise ValueError("capture requires a new, unused link started at zero")
    origin = _time(clock())
    writer = RecordingWriter(stream, started_at=0.)
    latest = 0.
    recorded = 0

    def elapsed() -> float:
        nonlocal latest
        try:
            now = _time(_time(clock()) - origin)
        except ValueError as exc:
            raise CaptureError("capture clock is not finite") from exc
        if now < latest:
            raise CaptureError("capture clock moved backwards")
        latest = now
        return now

    while True:
        now = elapsed()
        if stop_requested():
            reason = "requested"
            break
        if now >= duration:
            reason = "duration"
            break
        for event in link.poll(now):
            writer.append(event)
            recorded += 1
        report = link.report(now)
        if report.closed:
            raise CaptureError(f"link closed during capture: {report.last_error}")
        after_poll = elapsed()
        if after_poll < duration and not stop_requested():
            sleep(min(poll_interval, duration - after_poll))

    report = link.report(now)
    if report.closed:
        raise CaptureError(f"link closed during capture: {report.last_error}")
    writer.finish(now)
    return CaptureResult(now, recorded, reason, report.rx_bytes, report.bad_bytes)
