"""Describe paired detector observations without ground-truth accuracy claims.

Inputs are the evaluator's measured rows, in replay order. Display track IDs are
scoped to each source/size segment, since the production tracker is reset there.
An empty run describes sampled images only; it never estimates how long a person
was absent between captures. This module does not load models or image files.
"""
from __future__ import annotations

import math
from statistics import median


VARIANTS = ("tiny", "s")
STRONG_CONFIDENCE = .5
EMPTY_GAP_SECONDS = .7
MAX_EXAMPLES = 5


def _number(value, description, *, nonnegative=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (nonnegative and value < 0)):
        raise ValueError(f"{description} must be a finite {'nonnegative ' if nonnegative else ''}number")
    return float(value)


def _same_segment(previous, current):
    return (previous["segment"], previous["video_id"]) == (current["segment"], current["video_id"])


def _timing(values):
    if not values:
        return {"samples": 0, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {"samples": len(ordered), "median": float(median(ordered)),
            "p95": ordered[math.ceil(.95 * len(ordered)) - 1], "max": ordered[-1]}


def _validate(rows):
    for offset, row in enumerate(rows):
        if type(row.get("index")) is not int or row["index"] < 0:
            raise ValueError("row index must be a nonnegative integer")
        if type(row.get("segment")) not in (int, str) or not isinstance(row.get("video_id"), str):
            raise ValueError("row segment and video_id must identify its source segment")
        at = _number(row.get("at_s"), "row timestamp")
        if row.get("available_at_s") is not None:
            _number(row["available_at_s"], "replay cursor", nonnegative=True)
        if offset and _same_segment(rows[offset - 1], row) and at <= rows[offset - 1]["at_s"]:
            raise ValueError("row timestamps must increase within a segment")
        for variant in VARIANTS:
            model = row["models"][variant]
            for field in ("inference_ms", "processing_ms"):
                _number(model.get(field), f"{variant} {field}", nonnegative=True)
            if not isinstance(model.get("detections"), list):
                raise ValueError("model detections must be a list")
            for detection in model["detections"]:
                confidence = _number(detection.get("confidence"), "detection confidence")
                if not 0 <= confidence <= 1:
                    raise ValueError("detection confidence must be between 0 and 1")
                if type(detection.get("track_id")) is not int or detection["track_id"] < 1:
                    raise ValueError("display track_id must be a positive integer")


def summarize(rows: list[dict]) -> dict:
    """Summarize paired measurements; examples retain at most five first cases.

    Timings use an ordinary median and nearest-rank p95, with null values for
    an empty sample. Adjacent singleton ID changes are observations within one
    segment, including across a sampling gap; no identity truth is inferred.
    Empty runs additionally split when adjacent receptions are over 0.7 s apart.
    """
    if not isinstance(rows, list):
        raise ValueError("rows must be a list in replay order")
    _validate(rows)
    summary = {"frame_count": len(rows), "variants": {}, "pairwise": {
        "count_disagreement_frames": 0, "count_disagreement_examples": [],
    }}
    for variant in VARIANTS:
        with_detections = strong = changes = 0
        display_ids = set()
        inference, processing, empty_runs, change_examples = [], [], [], []
        current_empty = None
        previous = None
        for row in rows:
            model = row["models"][variant]
            detections = model["detections"]
            inference.append(float(model["inference_ms"]))
            processing.append(float(model["processing_ms"]))
            with_detections += bool(detections)
            strong += any(d["confidence"] >= STRONG_CONFIDENCE for d in detections)
            display_ids.update((row["segment"], row["video_id"], d["track_id"]) for d in detections)
            same_segment = (previous is not None and _same_segment(previous, row)
                            and not row.get("excluded_before", False))
            if detections:
                current_empty = None
            else:
                adjacent_empty = (current_empty is not None and same_segment
                                  and row["at_s"] - previous["at_s"] <= EMPTY_GAP_SECONDS)
                if not adjacent_empty:
                    current_empty = {"segment": row["segment"], "video_id": row["video_id"],
                                     "first_index": row["index"], "last_index": row["index"],
                                     "first_at_s": row["at_s"], "last_at_s": row["at_s"],
                                     "first_available_at_s": row.get("available_at_s"),
                                     "last_available_at_s": row.get("available_at_s"),
                                     "sample_count": 0}
                    empty_runs.append(current_empty)
                current_empty["last_index"] = row["index"]
                current_empty["last_at_s"] = row["at_s"]
                current_empty["last_available_at_s"] = row.get("available_at_s")
                current_empty["sample_count"] += 1
            if same_segment and len(detections) == 1:
                previous_detections = previous["models"][variant]["detections"]
                if (len(previous_detections) == 1
                        and previous_detections[0]["track_id"] != detections[0]["track_id"]):
                    changes += 1
                    if len(change_examples) < MAX_EXAMPLES:
                        change_examples.append({"segment": row["segment"], "video_id": row["video_id"],
                            "previous_index": previous["index"], "index": row["index"],
                            "previous_at_s": previous["at_s"], "at_s": row["at_s"],
                            "previous_available_at_s": previous.get("available_at_s"),
                            "available_at_s": row.get("available_at_s"),
                            "previous_track_id": previous_detections[0]["track_id"],
                            "track_id": detections[0]["track_id"],
                            "previous_confidence": previous_detections[0]["confidence"],
                            "confidence": detections[0]["confidence"]})
            previous = row
        summary["variants"][variant] = {
            "frame_count": len(rows), "frames_with_detections": with_detections,
            "frames_without_detections": len(rows) - with_detections,
            "frames_with_strong_detections": strong,
            "distinct_display_ids": len(display_ids),
            "adjacent_single_detection_id_changes": changes,
            "timing_ms": {"inference": _timing(inference), "processing": _timing(processing)},
            "empty_runs": {"count": len(empty_runs),
                           "max_sample_count": max((r["sample_count"] for r in empty_runs), default=0),
                           "examples": empty_runs[:MAX_EXAMPLES]},
            "id_change_examples": change_examples,
        }
    pairwise = summary["pairwise"]
    for row in rows:
        tiny_count, s_count = (len(row["models"][variant]["detections"]) for variant in VARIANTS)
        if tiny_count != s_count:
            pairwise["count_disagreement_frames"] += 1
            if len(pairwise["count_disagreement_examples"]) < MAX_EXAMPLES:
                pairwise["count_disagreement_examples"].append({
                    "index": row["index"], "at_s": row["at_s"], "segment": row["segment"],
                    "available_at_s": row.get("available_at_s"),
                    "tiny_count": tiny_count, "s_count": s_count,
                })
    return summary


def _text(value):
    text = " ".join(str(value).split())
    for character in ("\\", "|", "`", "<", ">", "[", "]"):
        text = text.replace(character, "\\" + character)
    return text


def _value(value):
    if value is None:
        return "unavailable"
    return f"{value:.3f}" if type(value) is float else _text(value)


def render_markdown(report: dict) -> str:
    """Render the evaluator's report envelope with explicit sampling limits."""
    summary, source = report["summary"], report["source"]
    config, models = report["configuration"], report["models"]
    tiny, small = (summary["variants"][v] for v in VARIANTS)
    labels = [_text(models[v]["label"]) for v in VARIANTS]
    lines = ["# Recorded-image vision comparison", "",
             f"Source: {_text(source['id'])} ({_text(source['environment'])}). "
             f"Report state: {_text(report['state'])}.", "",
             f"Archive duration: {_value(source['duration_s'])} s. "
             f"Archive frame rows: {_value(source['archive_frame_rows'])}; "
             f"selected frames: {_value(source['selected_frames'])}; "
             f"measured paired frames: {summary['frame_count']}.", "",
             f"Inference threads: {_value(config['threads'])}; "
             f"warm-up frames per model: {_value(config['warmup_frames'])}. "
             f"Selection truncated: {'yes' if source['truncated'] else 'no'}.", ""]
    skipped = source.get("skipped_counts", {})
    if skipped:
        lines += ["Skipped archive rows: " + "; ".join(
            f"{_text(reason)}: {_value(count)}" for reason, count in sorted(skipped.items())) + ".", ""]
    lines += ["Each pair uses the same archived JPEG. The production detector/tracker "
              "settings are retained, with independent tracking state per model.", "",
              f"| Observation | {labels[0]} | {labels[1]} |", "| --- | ---: | ---: |"]
    for label, key in (("Measured frames", "frame_count"),
                       ("Frames with detections", "frames_with_detections"),
                       ("Frames without detections", "frames_without_detections"),
                       ("Frames with a detection confidence ≥ 0.5", "frames_with_strong_detections"),
                       ("Distinct display IDs, scoped by segment", "distinct_display_ids"),
                       ("Adjacent singleton ID-change observations", "adjacent_single_detection_id_changes")):
        lines.append(f"| {label} | {tiny[key]} | {small[key]} |")
    for label, key in (("Empty sample runs", "count"), ("Largest empty run, samples", "max_sample_count")):
        lines.append(f"| {label} | {tiny['empty_runs'][key]} | {small['empty_runs'][key]} |")
    for label, key in (("Inference", "inference"), ("Processing", "processing")):
        for statistic in ("median", "p95", "max"):
            values = [_value(item["timing_ms"][key][statistic]) for item in (tiny, small)]
            lines.append(f"| {label} {statistic}, ms | {values[0]} | {values[1]} |")
    lines += ["", f"Detection counts differ on {summary['pairwise']['count_disagreement_frames']} "
              "paired frames. Counts alone do not determine which output is correct; "
              "track IDs are not matched between models.", "",
              "Timing p95 uses the nearest-rank method. These offline timings do not "
              "measure real-time FPS or end-to-end live video latency.", "",
              "## Sample observations", "",
              "Replay cursor (s) is the archived image's availability offset, suitable "
              "for seeking native replay. Receipt timestamps remain in the JSON and may "
              "predate recording start. Lists show at most five first examples per category. "
              "Frame indexes refer to [frames.jsonl](frames.jsonl).", ""]
    for variant in VARIANTS:
        item = summary["variants"][variant]
        label = _text(models[variant]["label"])
        lines += [f"### {label}", "", "Empty sample runs:", ""]
        for run in item["empty_runs"]["examples"]:
            lines.append(f"- Segment {_text(run['segment'])}, frames {run['first_index']}–{run['last_index']}: "
                         f"{run['sample_count']} empty samples; Replay cursor (s): "
                         f"{_value(run['first_available_at_s'])} → {_value(run['last_available_at_s'])}.")
        if not item["empty_runs"]["examples"]:
            lines.append("- None observed.")
        lines += ["", "Adjacent singleton ID-change observations:", ""]
        for change in item["id_change_examples"]:
            lines.append(f"- Segment {_text(change['segment'])}, frames {change['previous_index']} → "
                         f"{change['index']}: display ID {change['previous_track_id']} → {change['track_id']}; "
                         f"confidence {_value(change['previous_confidence'])} → {_value(change['confidence'])}; "
                         f"Replay cursor (s): {_value(change['available_at_s'])}.")
        if not item["id_change_examples"]:
            lines.append("- None observed.")
        lines.append("")
    lines += ["### Count disagreements", ""]
    examples = summary["pairwise"]["count_disagreement_examples"]
    for example in examples:
        lines.append(f"- Frame {example['index']}, segment {_text(example['segment'])}, "
                     f"Replay cursor (s): {_value(example['available_at_s'])}: "
                     f"Tiny {example['tiny_count']}, S {example['s_count']} detections.")
    if not examples:
        lines.append("- None observed.")
    lines += ["", "## Interpretation and limits", "",
              "Display IDs are temporary tracker labels, counted separately after each "
              "source/size segment reset. They are not a count of people. An ID change "
              "between adjacent single-detection samples is an observation, not a verified "
              "identity switch; sampling gaps may separate those rows.", "",
              "Empty runs split at source/segment changes and reception gaps over 0.7 s. "
              "Only the number and timestamps of empty samples are reported. Unrecorded "
              "intervals do not establish continuous absence. An excluded distinct "
              "non-increasing image also breaks empty runs and adjacent ID comparisons.", "",
              "No annotated ground truth is supplied. This report does not calculate "
              "accuracy or recall, or declare a model winner.", "",
              "Native replay retains its original archived overlays. Recomputed Tiny/S "
              "detections and IDs are in frames.jsonl; this comparison does not replace "
              "the recording's overlays.", ""]
    for limit in report.get("limits", []):
        lines.append("- " + _text(limit))
    lines += ["", "## Model provenance", "",
              "| Variant | Input size | SHA-256 |", "| --- | ---: | --- |"]
    for variant in VARIANTS:
        model = models[variant]
        lines.append(f"| {_text(model['label'])} | {_text(model['input_size'])} | {_text(model['sha256'])} |")
    return "\n".join(lines).rstrip() + "\n"
