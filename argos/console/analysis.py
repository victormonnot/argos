"""Offline timing of recorded receptions, independent of payload interpretation.

Rates and ages describe local receipt times for the selected source/type. They
cannot establish physical sensor age, radio quality, or how many packets were
lost. The recording must already have passed its integrity/codec validation.
"""
from collections import Counter
import heapq
import math


MAX_BINS = 400
MAX_GAPS = 100


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _identifier(value, maximum, name):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                              or not 0 <= value <= maximum):
        raise ValueError(f"{name} must be an integer in 0..{maximum}")


def _rate(count, duration):
    result = count / duration
    if not math.isfinite(result):
        raise ValueError("recording duration is too short for a finite rate")
    return result


def analyze_recording(recording, *, system=None, component=None, message_id=None,
                      bins=160, gap_threshold_s=1.0):
    """Summarize one validated journal in one pass, without decoding frames.

    Bins are left-inclusive/right-exclusive, except the final endpoint. The
    maximum age includes the peak immediately before a receipt resets it; a
    reset exactly on a bin boundary belongs to the following bin. Before the
    first matching receipt, age is unknown rather than time since capture start.

    Gap boundaries delimit observed silence, including initial/final silence.
    Keep the 100 longest qualifying gaps, longest first, breaking ties by start
    time. ``max_gap_s`` includes gaps below the display threshold. Menu counts
    cover the whole recording in first-seen order, independently of filters.

    Work is O(events + bins); the bounded gap heap does not grow with the log.
    Only the source/type counters grow with the number of distinct identities.
    """
    _identifier(system, 255, "system")
    _identifier(component, 255, "component")
    _identifier(message_id, 0xffffff, "message_id")
    if (system is None) != (component is None):
        raise ValueError("system and component must be supplied together")
    if isinstance(bins, bool) or not isinstance(bins, int) or not 1 <= bins <= MAX_BINS:
        raise ValueError(f"bins must be an integer in 1..{MAX_BINS}")
    threshold = _finite(gap_threshold_s, "gap_threshold_s")
    if not 0 < threshold <= 86400:
        raise ValueError("gap_threshold_s must be positive and no greater than 86400")
    start = _finite(recording.started_at, "started_at")
    end = _finite(recording.ended_at, "ended_at")
    if start < 0 or end < start:
        raise ValueError("recording times must be nonnegative and ordered")
    duration = end - start
    edges = [duration * (index / bins) for index in range(bins + 1)] if duration else [0., 0.]
    if duration and any(right <= left for left, right in zip(edges, edges[1:])):
        raise ValueError("recording duration cannot represent the requested bins")
    buckets = [{"start_s": left, "end_s": right, "events": 0,
                "rate_hz": None, "max_age_s": None}
               for left, right in zip(edges, edges[1:])]
    source_counts, type_counts = Counter(), Counter()
    type_names = {}
    longest = []
    gap_count = 0
    max_gap = 0.
    matched = 0
    previous = None
    last_recorded = start
    index = 0

    def gap(left, right, boundary):
        nonlocal gap_count, max_gap
        length = right - left
        max_gap = max(max_gap, length)
        if length <= 0 or length < threshold:
            return
        gap_count += 1
        # The worst retained gap is shortest, and latest on equal lengths.
        entry = (length, -left, right, boundary)
        if len(longest) < MAX_GAPS:
            heapq.heappush(longest, entry)
        elif entry > longest[0]:
            heapq.heapreplace(longest, entry)

    def peak(bucket, at):
        if previous is not None:
            age = at - previous
            current = bucket["max_age_s"]
            bucket["max_age_s"] = age if current is None else max(current, age)

    for event in recording.events:
        received = _finite(event.received_at, "received_at")
        if not last_recorded <= received <= end:
            raise ValueError("recorded receipts must be ordered and inside the recording")
        last_recorded = received
        source_counts[event.system, event.component] += 1
        type_counts[event.message_id] += 1
        type_names.setdefault(event.message_id, event.type_name)
        if ((system is not None and (event.system != system or event.component != component))
                or (message_id is not None and event.message_id != message_id)):
            continue
        at = received - start
        gap(0. if previous is None else previous, at, "start" if previous is None else "interior")
        while index < len(buckets) - 1 and at >= buckets[index]["end_s"]:
            peak(buckets[index], buckets[index]["end_s"])
            index += 1
        bucket = buckets[index]
        if at > bucket["start_s"]:
            peak(bucket, at)
        bucket["events"] += 1
        if bucket["max_age_s"] is None:
            bucket["max_age_s"] = 0.
        previous = at
        matched += 1

    gap(0. if previous is None else previous, duration, "whole" if previous is None else "end")
    for bucket in buckets[index:]:
        peak(bucket, bucket["end_s"])
    if duration:
        for bucket in buckets:
            bucket["rate_hz"] = _rate(bucket["events"], bucket["end_s"] - bucket["start_s"])
    return {
        "duration_s": duration,
        "filter": {"system": system, "component": component, "message_id": message_id},
        "events": matched, "bins": buckets,
        "gaps": [{"start_s": -negative_start, "end_s": right,
                  "duration_s": length, "boundary": boundary}
                 for length, negative_start, right, boundary in sorted(longest, reverse=True)],
        "gap_count": gap_count, "gaps_truncated": gap_count > MAX_GAPS,
        "gap_threshold_s": threshold,
        "summary": {"mean_rate_hz": _rate(matched, duration) if duration else None,
                    "max_gap_s": max_gap},
        "sources": [{"system": pair[0], "component": pair[1], "events": count}
                    for pair, count in source_counts.items()],
        "message_types": [{"message_id": kind, "type_name": type_names[kind], "events": count}
                          for kind, count in type_counts.items()],
    }
