"""Bounded, explicitly reviewed reference checks for offline continuity replays.

Assistant review is never promoted to human truth. Annotated candidate removal
is a selection-only counterfactual, not an implementable deduplication policy.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math

from .image_tracks import _iou


CONDITIONS = ("unchanged", "duplicates_only", "false_positives_only", "both")
CLASSIFICATIONS = {"target", "other_person", "not_person", "unknown"}
VISIBILITY = {"visible", "partial", "occluded", "offscreen", "uncertain"}


def _number(value):
    return type(value) in (float, int) and math.isfinite(value)


def _box(value):
    return (isinstance(value, list) and len(value) == 4 and all(_number(v) for v in value)
            and min(value) >= 0 and min(value[2:]) > 0
            and value[0] + value[2] <= 1 and value[1] + value[3] <= 1)


def validate_reference(reference, frames, window):
    """Require every source image in the declared span, with exact image binding."""
    if (not isinstance(reference, dict) or reference.get("format") != "argos.continuity.dense-reference"
            or type(reference.get("version")) is not int or reference["version"] != 1
            or reference.get("window") != window["name"]
            or reference.get("review_status") != "assistant_reviewed"
            or reference.get("human_validated") is not False
            or not isinstance(reference.get("reviewer"), str) or not reference["reviewer"].strip()):
        raise ValueError("reference must explicitly identify assistant review, without human validation")
    start, end = reference.get("start"), reference.get("end")
    if (not _number(start) or not _number(end)
            or not window["start"] <= start < end <= window["end"]):
        raise ValueError("reference span must be inside the replay window")
    expected = {frame["sequence"]: frame for frame in frames if start <= frame["received_at"] <= end}
    rows = reference.get("frames")
    if not expected or not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("reference must cover every source image in its span")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get("sequence")) is not int:
            raise ValueError("invalid reference image")
        sequence = row["sequence"]
        frame = expected.get(sequence)
        if (sequence in seen or frame is None or row.get("sha256") != frame["sha256"]
                or row.get("received_at") != frame["received_at"]):
            raise ValueError("reference image/timestamp binding mismatch")
        seen.add(sequence)
        target = row.get("target")
        if (row.get("review_status") != "assistant_reviewed" or not isinstance(target, dict)
                or target.get("identity") != "person-1" or target.get("visibility") not in VISIBILITY
                or target.get("spatial_quality") not in {"clear", "uncertain"}):
            raise ValueError("reference must declare target identity, visibility and uncertainty")
        box = target.get("box")
        if box is not None and not _box(box):
            raise ValueError("reference box must be normalized xywh or null")
        if (target["visibility"] in {"visible", "partial"} and target["spatial_quality"] == "clear"
                and box is None):
            raise ValueError("clear visible reference requires a measured visible-extent box")
        if "other_people" not in row:
            raise ValueError("other_people must explicitly declare reviewed entries or unknown null")
        others = row["other_people"]
        if others is not None:
            if not isinstance(others, list):
                raise ValueError("other_people must be reviewed entries or unknown null")
            identities = set()
            for person in others:
                if (not isinstance(person, dict) or not isinstance(person.get("identity"), str)
                        or not person["identity"] or person["identity"] == "person-1"
                        or person["identity"] in identities or not _box(person.get("box"))):
                    raise ValueError("other person needs a distinct identity and visible-extent box")
                identities.add(person["identity"])
    if seen != set(expected):
        raise ValueError("reference coverage mismatch")
    return {row["sequence"]: deepcopy(row) for row in rows}


def validate_judgments(review, frames, reference, *, reference_sha256, cache_sha256):
    """Bind manual candidate classes to the exact cached detector output."""
    if (not isinstance(review, dict) or review.get("format") != "argos.continuity.detection-review"
            or type(review.get("version")) is not int or review["version"] != 1
            or review.get("review_status") != "assistant_reviewed"
            or review.get("human_validated") is not False
            or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()
            or review.get("reference_sha256") != reference_sha256
            or review.get("cache_sha256") != cache_sha256):
        raise ValueError("candidate review must bind the frozen reference and inference cache")
    rows, source = review.get("frames"), {frame["sequence"]: frame for frame in frames}
    if not isinstance(rows, list) or len(rows) != len(reference):
        raise ValueError("candidate review must cover all reference images, including empty detections")
    checked = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("sequence")) is not int
                or row["sequence"] not in reference or row["sequence"] in checked):
            raise ValueError("candidate review image missing, unknown or duplicated")
        sequence = row["sequence"]
        frame, annotations = source[sequence], row.get("detections")
        if (row.get("frame_sha256") != frame["sha256"] or not isinstance(annotations, list)
                or len(annotations) != len(frame["detections"])):
            raise ValueError("candidate review must cover the exact source detections")
        indices, labels = set(), []
        for annotation in annotations:
            if not isinstance(annotation, dict):
                raise ValueError("invalid candidate annotation")
            index = annotation.get("detection_index")
            if type(index) is not int or index in indices or not 0 <= index < len(frame["detections"]):
                raise ValueError("candidate detection indices must be unique and complete")
            indices.add(index)
            measured = frame["detections"][index]
            if (not _box(annotation.get("box")) or annotation["box"] != measured["box"]
                    or not _number(annotation.get("confidence"))
                    or annotation["confidence"] != measured["confidence"]):
                raise ValueError("candidate box/score binding mismatch")
            kind, person = annotation.get("classification"), annotation.get("person_id")
            others = reference[sequence]["other_people"]
            if (kind not in CLASSIFICATIONS or kind == "target" and person != "person-1"
                    or kind == "other_person" and (others is None or person not in {p["identity"] for p in others})
                    or kind in {"not_person", "unknown"} and person is not None
                    or not isinstance(annotation.get("evidence_id"), str) or not annotation["evidence_id"].strip()):
                raise ValueError("candidate classification/person/evidence is inconsistent")
            if (reference[sequence]["target"]["visibility"] in {"uncertain", "occluded", "offscreen"}
                    and kind == "target"):
                raise ValueError("uncertain or invisible target cannot become a confirmed candidate")
            labels.append(deepcopy(annotation))
        checked[sequence] = sorted(labels, key=lambda a: a["detection_index"])
    return checked


def make_suppressions(condition, judgments, frames):
    """Fixed oracle: keep highest-confidence target (source index breaks ties).

    Unknowns and other people always remain. A selected lower-confidence duplicate
    may be removed: the oracle has no access to tracker IDs or recovery outcomes.
    """
    if condition not in CONDITIONS:
        raise ValueError("unknown annotated counterfactual condition")
    source = {frame["sequence"]: frame for frame in frames}
    requests = []
    for sequence, annotations in sorted(judgments.items()):
        targets = [item for item in annotations if item["classification"] == "target"]
        keep = min(targets, key=lambda a: (-a["confidence"], a["detection_index"])) if targets else None
        for item in annotations:
            reason = None
            if (condition in {"duplicates_only", "both"} and item["classification"] == "target"
                    and item is not keep):
                reason = "reviewed_duplicate"
            if condition in {"false_positives_only", "both"} and item["classification"] == "not_person":
                reason = "reviewed_false_positive"
            if reason:
                requests.append(dict(sequence=sequence, frame_sha256=source[sequence]["sha256"],
                    detection_index=item["detection_index"], expected_box=deepcopy(item["box"]),
                    expected_confidence=item["confidence"], reason=reason, evidence_id=item["evidence_id"]))
    return requests


def _software_durations(events, start, end, kind, value, default):
    current, previous, durations = default, start, Counter()
    for event in events:
        if event["kind"] != kind:
            continue
        at = event["at"]
        if at <= start:
            current = event[value]
        elif at <= end:
            durations[str(current)] += at - previous
            previous, current = at, event[value]
    durations[str(current)] += end - previous
    return dict(durations)


def evaluate_replay(run, reference, judgments, *, start, end):
    """Describe exact admitted-frame agreement, keeping misses and unknowns apart."""
    decisions = {item["sequence"]: item for item in run["frame_decisions"]}
    rows, counts = [], Counter()
    for sequence, ref in sorted(reference.items()):
        decision = decisions[sequence]
        annotations = judgments[sequence]
        target = ref["target"]
        eligible = target["visibility"] in {"visible", "partial"} and target["spatial_quality"] == "clear"
        admitted = decision["status"] == "accepted"
        counts["reference_source_images"] += 1
        counts["clear_visible_source_images"] += eligible
        counts["admitted_reference_images"] += admitted
        counts["eligible_admitted_images"] += eligible and admitted
        row = dict(sequence=sequence, received_at=ref["received_at"], reference_eligible=eligible,
                   admission=decision["status"], selected_classification=None, selected_reference_iou=None,
                   target_detection_indices=[a["detection_index"] for a in annotations if a["classification"] == "target"],
                   strong_target_detection_indices=[a["detection_index"] for a in annotations
                                                    if a["classification"] == "target" and a["confidence"] >= .5])
        if not admitted:
            row["outcome"] = "not_admitted"
        elif not eligible:
            row["outcome"] = "reference_uncertain_or_not_visible"
        else:
            preview = decision["preview"]
            effective = decision.get("preview_detections", decision["detections"])
            indices = decision.get("preview_detection_indices", list(range(len(decision["detections"]))))
            selected = [(d, index) for d, index in zip(effective, indices)
                        if d["track_id"] == preview["target_id"] and d["confidence"] >= .5
                        and preview["phase"] == "tracking"]
            row.update(phase=preview["phase"], consumer_valid=decision["consumer"]["valid"],
                       selected_track_id=preview["target_id"], preview_detection_indices=indices)
            if len(selected) > 1:
                raise ValueError("selected identity maps to multiple current observations")
            if selected:
                observed, index = selected[0]
                label = annotations[index]["classification"]
                row.update(selected_classification=label, selected_detection_index=index,
                           selected_reference_iou=_iou(observed["box"], target["box"]),
                           outcome=f"selected_{label}_by_review")
            elif not row["target_detection_indices"]:
                row["outcome"] = ("unresolved_candidate_review" if any(a["classification"] == "unknown" for a in annotations)
                                  else "no_reviewed_target_detection")
            elif not row["strong_target_detection_indices"]:
                row["outcome"] = ("unresolved_strong_candidate_review" if any(
                    a["classification"] == "unknown" and a["confidence"] >= .5 for a in annotations)
                    else "only_weak_reviewed_target_detection")
            else:
                row["outcome"] = "strong_reviewed_target_not_selected"
            counts[row["outcome"]] += 1
        rows.append(row)
    return dict(rows=rows, counts=dict(counts),
                phase_seconds=_software_durations(run["events"], start, end, "preview_transition", "phase", "idle"),
                dry_valid_seconds=_software_durations(run["events"], start, end, "consumer_transition", "valid", False).get("True", 0.),
                terminal_stops=[deepcopy(event) for event in run["events"] if event["kind"] == "preview_transition"
                                and start <= event["at"] <= end and event["phase"] == "stopped"],
                meaning="Exact-image agreement with assistant review, not human-validated identity accuracy or time following the correct person")
