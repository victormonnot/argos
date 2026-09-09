#!/usr/bin/env python3
"""Compare framing admission policies on one recorded camera path, offline.

Input JSONL is ordered by availability time ``at`` on the session clock. Frame
rows have kind="frame", sequence, received_at, run_id, video_id, detections and
an optional jpeg path. The pixels are never loaded or inferred again. Optional
kind="tick" rows supply collector poll times (and optional vehicle_reason);
these are not the controller's execution times. Otherwise ticks are generated
every 50 ms. kind="state" rows are ignored.

Every explicit window is a separate operator engagement, with confidence .5
required at its start. A takeover never automatically starts another attempt.
An engaged window's tail after its last replay tick is reported as unevaluated;
no final tick, continued validity or loss is invented at the window boundary.
Windows JSON: [{"name":"trial", "start":10, "end":50, "target_id":7}].

    PYTHONPATH=. python examples/replay_framing.py observations.jsonl \
        --windows windows.json --availability-uncertainty .05 --output replay.json

This single-threaded program temporarily scopes two helper constants and runs
the production FramingControl/FramingLaw. It opens no transport and sends no
commands. A different policy would change an actual flight's camera path, so
replay evaluates admission and continuity, not closed-loop flight performance.
It assumes valid pilot authority within each explicit window; FlightControl's
lease, heartbeat, autopilot and landing behavior are not simulated here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

from argos.console import framing as lifecycle


ENGAGEMENT_CONFIDENCE = .5
DEFAULT_CONFIDENCES = (.5, .45, .4)
DEFAULT_PAUSES = (.35, .6, .8)
FRAME_FIELDS = ("sequence", "received_at", "run_id", "video_id", "detections")


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Replay times must be finite nonnegative numbers")
    return float(value)


@dataclass(frozen=True)
class Window:
    name: str
    start: float
    end: float
    target_id: int

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("An explicit window name is required")
        if _time(self.end) <= _time(self.start):
            raise ValueError("Window end must follow its start")
        if type(self.target_id) is not int or self.target_id < 1:
            raise ValueError("An explicit positive target ID is required")


@dataclass(frozen=True)
class Candidate:
    confidence: float
    pause: float

    def __post_init__(self):
        if not .35 <= _time(self.confidence) <= ENGAGEMENT_CONFIDENCE:
            raise ValueError("Continuation confidence must be between .35 and .5")
        if not 0 < _time(self.pause) <= 2:
            raise ValueError("Candidate pause must be positive and at most two seconds")


def validate_records(records):
    """Reject reordered availability times instead of silently sorting evidence."""
    previous = -1.
    for record in records:
        if not isinstance(record, dict) or record.get("kind") not in ("frame", "tick", "state"):
            raise ValueError("Each record needs kind frame, tick or state")
        at = _time(record.get("at"))
        if at < previous:
            raise ValueError("Records must be ordered by availability time")
        previous = at
    if not records:
        raise ValueError("No replay records")


@contextmanager
def _parameters(confidence, pause):
    """Offline, sequential use only; restore globals even after an exception."""
    old = lifecycle.MIN_CONFIDENCE, lifecycle.DETECTION_PAUSE
    lifecycle.MIN_CONFIDENCE, lifecycle.DETECTION_PAUSE = confidence, pause
    try:
        yield
    finally:
        lifecycle.MIN_CONFIDENCE, lifecycle.DETECTION_PAUSE = old


def _phase(state):
    if state["phase"] == "active":
        return "paused" if state["paused"] else "valid"
    return "takeover" if state["phase"] == "takeover" else "unengaged"


def _transition(at, state):
    return {"at": at, "phase": _phase(state), "reason": state["reason"],
            "frame_age_s": state["frame_age_s"], "axes": state["axes"]}


def _gaps(times):
    gaps = sorted(second - first for first, second in zip(times, times[1:]))
    if not gaps:
        return {"count": 0, "p95": None, "max": None}
    position = .95 * (len(gaps) - 1)
    index = int(position)
    p95 = gaps[index] + (gaps[min(index + 1, len(gaps) - 1)] - gaps[index]) * (position - index)
    return {"count": len(gaps), "p95": p95, "max": max(gaps)}


def replay(records, window: Window, candidate: Candidate, *, tick_seconds=.05):
    """Replay exactly one explicit engagement; loss remains latched to the end."""
    validate_records(records)
    if not 0 < _time(tick_seconds) <= .2:
        raise ValueError("Tick interval must be positive and at most .2 seconds")
    if window.start < records[0]["at"] or window.end > records[-1]["at"]:
        raise ValueError("Window must be covered by the recorded time range")
    frames = [record for record in records if record["kind"] == "frame"]
    recorded_ticks = any(record["kind"] == "tick" for record in records)
    if recorded_ticks:
        ticks = [record for record in records if record["kind"] == "tick"
                 and window.start < record["at"] <= window.end]
        if not ticks:
            raise ValueError("No recorded poll ticks inside this window")
    else:
        count = math.floor((window.end - window.start) / tick_seconds + 1e-10)
        ticks = [{"at": min(window.end, window.start + i * tick_seconds)}
                 for i in range(1, count + 1)]
    helper = lifecycle.FramingControl(enabled=True)
    cursor, latest = 0, None

    def observe_until(at):
        nonlocal cursor, latest
        while cursor < len(frames) and frames[cursor]["at"] <= at:
            latest = frames[cursor]
            # Repeated identities are admitted unchanged. The real helper
            # rejects mutated identities and never advances its law twice.
            helper.observe({key: latest[key] for key in FRAME_FIELDS if key in latest})
            cursor += 1

    durations = dict.fromkeys(("valid", "paused", "takeover", "unengaged", "unevaluated"), 0.)
    result = {"window": asdict(window), "candidate": asdict(candidate),
              "engagement_confidence": ENGAGEMENT_CONFIDENCE,
              "tick_source": "recorded_poll" if recorded_ticks else "generated",
              "replay_tick_gap_s": _gaps([window.start] + [tick["at"] for tick in ticks]),
              "tail_after_last_tick_s": window.end - (ticks[-1]["at"] if ticks else window.start),
              "tick_count": len(ticks), "engaged": False, "engagement_error": None,
              "duration_s": durations, "pause_recoveries": 0, "transitions": [],
              "takeover_deadline_reached_at": None, "first_loss": None,
              "max_abs_axes": dict.fromkeys(lifecycle.AXES, 0.)}
    observe_until(window.start)
    try:
        if latest is None:
            raise RuntimeError("No image was available at the explicit engagement time")
        with _parameters(ENGAGEMENT_CONFIDENCE, candidate.pause):
            helper.select(window.target_id, (latest.get("run_id"), latest.get("video_id")), window.start)
            helper.engage(window.start)
    except (RuntimeError, ValueError) as exc:
        result["engagement_error"] = str(exc)
        durations["unengaged"] = window.end - window.start
        return result
    result["engaged"] = True
    with _parameters(candidate.confidence, candidate.pause):
        previous = helper.state(window.start)
        result["transitions"].append(_transition(window.start, previous))
        last_at = window.start
        for tick in ticks:
            at = tick["at"]
            durations[_phase(previous)] += at - last_at
            observe_until(at)
            helper.tick(at, tick.get("vehicle_reason", ""))
            state = helper.state(at)
            if _phase(state) != _phase(previous):
                result["transitions"].append(_transition(at, state))
                if _phase(previous) == "paused" and _phase(state) == "valid":
                    result["pause_recoveries"] += 1
            if helper.takeover_due(at) and result["takeover_deadline_reached_at"] is None:
                result["takeover_deadline_reached_at"] = at
            for axis, value in state["axes"].items():
                result["max_abs_axes"][axis] = max(result["max_abs_axes"][axis], abs(value))
            previous, last_at = state, at
        # State-only records can extend the file beyond the last poll. Do not
        # extrapolate its last phase through that unprocessed tail: an image
        # could become stale or a pause expire without any replay tick there.
        durations["unevaluated"] = window.end - last_at
        result["first_loss"] = helper.last_loss
    return result


def compare(records, windows, *, confidences=DEFAULT_CONFIDENCES, pauses=DEFAULT_PAUSES,
            availability_uncertainty=.2):
    validate_records(records)
    uncertainty = _time(availability_uncertainty)
    if not windows or len({window.name for window in windows}) != len(windows):
        raise ValueError("Provide explicit windows with distinct names")
    return {
        "schema_version": 1,
        "helper_sha256": hashlib.sha256(Path(lifecycle.__file__).read_bytes()).hexdigest(),
        "availability_uncertainty_s": uncertainty,
        "recorded_poll_gap_s": _gaps([record["at"] for record in records if record["kind"] == "tick"]),
        "limitations": [
            "received_at is the known camera receipt timestamp; at is when the collector observed an analyzed result, not its exact server publication time.",
            "Recorded tick rows are collector polls, not FramingControl execution times; server scheduling and publication may precede the poll.",
            "Borderline recoveries may change within availability uncertainty, measured poll gaps and execution phase; replay does not resolve that uncertainty.",
            "Frames are fixed to the recorded flight: this is an admission replay, not a simulation of the alternative flight path.",
            "Explicit windows assume valid pilot authority; vehicle dynamics, browser leases and LAND execution are not simulated.",
            "Every window is an independent explicit engagement; a loss is never silently restarted within a window.",
            "Replay executes the helper at the supplied poll times; generated ticks instead use a 50 ms interval anchored at engagement.",
            "For engaged windows, time after the last replay tick is unevaluated, not continued validity or an inferred loss.",
        ],
        "results": [replay(records, window, Candidate(confidence, pause))
                    for window in windows for confidence in confidences for pause in pauses],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("--windows", type=Path, required=True)
    parser.add_argument("--availability-uncertainty", type=float, default=.2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        raw = args.input.read_bytes()
        records = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        windows = [Window(**item) for item in json.loads(args.windows.read_text())]
        report = compare(records, windows, availability_uncertainty=args.availability_uncertainty)
        report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    output = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
