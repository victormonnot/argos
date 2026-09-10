"""Descriptive, bounded summaries of already-recorded framing samples.

These are local receipt-time observations, not a re-simulation or an estimate
of target identity, position, distance or flight safety. A sample supports a
state only until the next sample or the archive's recorded age limit. Missing
metadata and missing coverage stay unknown, including legacy profile choices.
"""
from __future__ import annotations

import math


MAX_SERIES = 1200
MAX_INTERVALS = 1500
MAX_REPORT_EVENTS = 500
STATES = ("manual", "full", "pilot_throttle", "unknown_profile", "paused",
          "takeover", "inactive", "unknown")
PROFILES = ("full", "pilot_throttle")
RESPONSES = ("gentle", "normal", "responsive")


def _number(value, low=0., high=1e15):
    try:
        return (float(value) if type(value) in (float, int)
                and math.isfinite(value) and low <= value <= high else None)
    except (OverflowError, ValueError):
        return None


def _text(value):
    return value[:400] if isinstance(value, str) else ""


def _choice(value, choices):
    return value if isinstance(value, str) and value in choices else None


def _describe(control):
    """Do not turn an absent controller/ownership field into manual flight."""
    control = control if isinstance(control, dict) else {}
    framing = control.get("framing")
    framing = framing if isinstance(framing, dict) else {}
    vehicle = control.get("vehicle")
    vehicle = vehicle if isinstance(vehicle, dict) else {}
    profile = _choice(framing.get("profile"), PROFILES)
    phase = _choice(framing.get("phase"), ("idle", "selected", "active", "takeover", "disabled"))
    owned, armed, enabled = control.get("owned"), vehicle.get("armed"), control.get("enabled")
    control_phase = control.get("phase")
    state = "unknown"
    if owned is False or armed is False or enabled is False:
        state = "inactive"
    elif owned is True and armed is True and enabled is True:
        if control_phase != "armed":
            state = "inactive" if control_phase in ("landing", "switching", "recovering") else "unknown"
        elif phase == "takeover":
            state = "takeover"
        elif phase == "active":
            if framing.get("paused") is True:
                state = "paused"
            elif framing.get("paused") is False:
                state = profile or "unknown_profile"
        elif phase in ("idle", "selected", "disabled"):
            state = "manual"
    target = framing.get("target_id")
    target = target if type(target) is int and 1 <= target <= 2**31 - 1 else None
    return {"state": state, "profile": profile,
            "range_response": _choice(framing.get("range_response"), RESPONSES),
            "target_id": target,
            "reference_height": _number(framing.get("reference_height"), .001, 1.),
            "reason": _text(framing.get("reason")) if state in ("paused", "takeover") else ""}, framing


def _reduce_series(points, duration):
    if len(points) <= MAX_SERIES:
        return points
    # First/last and both signed extrema of each signal survive each time bin.
    # A segment number prevents plotting a line across an omitted state change.
    bins = MAX_SERIES // 6
    groups = [[] for _ in range(bins)]
    for index, point in enumerate(points):
        bucket = min(bins - 1, int(point["at_s"] / duration * bins)) if duration else 0
        groups[bucket].append(index)
    selected = set()
    for group in groups:
        if not group:
            continue
        selected.update((group[0], group[-1]))
        for name in ("error_x", "size_error"):
            measured = [index for index in group if points[index][name] is not None]
            if measured:
                selected.add(min(measured, key=lambda index: points[index][name]))
                selected.add(max(measured, key=lambda index: points[index][name]))
    return [points[index] for index in sorted(selected)]


def framing_report(samples, events, metadata):
    """Summarize validated rows, streaming control JSON outside this function.

    Samples are (index, absolute sample time, public control, image receipt or
    None). Events contain relative at_s. The archive owns validation, file
    revision fencing and thread serialization; this function never opens IO.
    """
    start, visual_end = metadata["started_at"], metadata["ended_at"]
    end = start + metadata.get("session_duration_s", visual_end - start)
    duration = end - start
    age_limit = metadata["sample_age_limit_s"]
    image_age_limit = metadata["video_age_limit_s"]
    totals = dict.fromkeys(STATES, 0.)
    intervals, points, interruptions = [], [], []
    interval_count = sample_count = metric_count = interruption_count = 0
    metric_duration = 0.
    previous_interval = None
    last_segment_key = None
    last_metric_end = None
    segment = 0
    last_interruption = None

    def interval(begin, finish, description):
        nonlocal previous_interval, interval_count
        if finish <= begin:
            return
        totals[description["state"]] += finish - begin
        key = tuple(description.items())
        if previous_interval is not None and previous_interval[0] == key and abs(previous_interval[1]["end_s"] - begin) < 1e-8:
            previous_interval[1]["end_s"] = finish
            return
        value = {"start_s": begin, "end_s": finish, **description}
        interval_count += 1
        if len(intervals) < MAX_INTERVALS:
            intervals.append(value)
        previous_interval = key, value

    unknown = {"state": "unknown", "profile": None, "range_response": None,
               "target_id": None, "reference_height": None, "reason": "No recent recorded control sample"}

    def consume(row, next_at):
        nonlocal sample_count, metric_count, metric_duration, segment, last_segment_key, last_metric_end
        nonlocal last_interruption, interruption_count
        index, at, control, image_received = row
        sample_count += 1
        at_s = at - start
        finish = min(next_at, at + age_limit, visual_end)
        description, framing = _describe(control)
        interval(at_s, finish - start, description)
        interval(finish - start, next_at - start, unknown)
        error_x = _number(framing.get("error_x"), -1., 1.)
        error_y = _number(framing.get("error_y"), -1., 1.)
        height = _number(framing.get("height"), .000001, 1.)
        reference = description["reference_height"]
        frame_age = _number(framing.get("frame_age_s"), 0., image_age_limit)
        image_age = None if image_received is None else _number(at - image_received, 0., image_age_limit)
        valid = (description["state"] in ("full", "pilot_throttle", "unknown_profile")
                 and all(value is not None for value in (error_x, height, reference, frame_age, image_age)))
        metric_end = (min(finish, at + image_age_limit - frame_age,
                          image_received + image_age_limit) if valid else at)
        key = tuple(description.items())
        if key != last_segment_key or not valid or last_metric_end is None or at > last_metric_end + 1e-8:
            segment += 1
        points.append({"at_s": at_s, "error_x": error_x if valid else None,
                       "error_y": error_y if valid else None,
                       "height": height if valid else None, "reference_height": reference,
                       "size_error": (height - reference) / reference if valid else None,
                       "profile": description["profile"], "range_response": description["range_response"],
                       "target_id": description["target_id"], "segment": segment, "valid": valid})
        if valid:
            metric_count += 1
            metric_duration += max(0., metric_end - at)
        last_segment_key, last_metric_end = key, metric_end

        interruption = control.get("interruption") if isinstance(control, dict) else None
        if isinstance(interruption, dict):
            happened = _number(interruption.get("at"), start, at)
            reason = _text(interruption.get("reason"))
            identity = happened, reason
            if happened is not None and reason and identity != last_interruption:
                interruption_count += 1
                interruptions.append({"index": f"interruption-{index}", "at_s": happened - start,
                                      "kind": "interruption", "detail": reason,
                                      "status": "recorded", "source": "control_sample"})
                if len(interruptions) > MAX_REPORT_EVENTS:
                    interruptions.pop(0)
                last_interruption = identity

    previous = None
    for row in samples:
        if previous is None:
            interval(0., row[1] - start, unknown)
        else:
            consume(previous, row[1])
        previous = row
    if previous is None:
        interval(0., duration, unknown)
    else:
        consume(previous, end)
    combined_events = sorted([*events, *interruptions], key=lambda event: (event["at_s"], str(event["index"])))
    event_count = metadata["events"] + interruption_count
    series = _reduce_series(points, duration)
    return {"state": "complete", "duration_s": duration, "visual_duration_s": visual_end - start,
            "coverage": {"sampled_s": duration - totals["unknown"], "unknown_s": totals["unknown"],
                         "metric_s": metric_duration, "samples": sample_count, "metric_samples": metric_count},
            "durations_s": totals, "intervals": intervals, "intervals_count": interval_count,
            "intervals_truncated": interval_count > MAX_INTERVALS,
            "series": series, "series_count": len(points), "series_downsampled": len(series) < len(points),
            "events": combined_events[-MAX_REPORT_EVENTS:], "events_count": event_count,
            "events_truncated": event_count > MAX_REPORT_EVENTS, "interruptions_count": interruption_count,
            "limits": {"series": MAX_SERIES, "intervals": MAX_INTERVALS, "events": MAX_REPORT_EVENTS,
                       "sample_age_s": age_limit, "video_age_s": image_age_limit}}
