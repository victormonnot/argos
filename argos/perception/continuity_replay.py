"""Offline selected-person diagnostics using the existing production boundaries.

Inputs are cached image measurements, never devices or transport endpoints.
Recorded observation times are only logging-time proxies for result availability;
the synthetic mode exercises the real latest-image VisionService scheduler with a
fake worker and declared cached work durations. Neither reproduces a flight.
"""
from __future__ import annotations

from collections import Counter
from copy import copy, deepcopy
from dataclasses import asdict
import hashlib
import heapq
import json
import math
from pathlib import Path
from queue import Empty, Queue
import time
from types import SimpleNamespace

from argos.backends.vision_bench_source import PreviewError
from argos.backends.yaw_stream_source import YawValidator
from argos.console.video import VideoSample
from argos.console.vision import AnalyzedFrame, VisionService
from argos.console.yaw_preview import YawPreview
from argos.perception.appearance import validate_appearances
from argos.perception.image_tracks import ImageTracker, _iou


RUN_ID = "offline-run"
VIDEO_ID = "offline-camera"
ENDPOINT = "/dev/video0"  # Contract metadata only; this module never opens it.


def _number(value, name, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or (positive and value == 0)):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return float(value)


def _inputs(frames, start, end, selection, mode, max_hz, tick_seconds, extra_worker_ms):
    start, end = _number(start, "start"), _number(end, "end")
    if end <= start:
        raise ValueError("end must be after start")
    if mode not in ("recorded", "simulated"):
        raise ValueError("mode must be recorded or simulated")
    # Reuse the same supported rate validator as production.
    from argos.console.config import validate_vision_hz
    max_hz = validate_vision_hz(max_hz)
    tick_seconds = _number(tick_seconds, "tick_seconds", positive=True)
    extra_worker_ms = _number(extra_worker_ms, "extra_worker_ms")
    if tick_seconds > .1 or (end - start) / tick_seconds > 1_000_000:
        raise ValueError("tick schedule must be bounded and at most 100 ms apart")
    if mode == "recorded" and extra_worker_ms:
        raise ValueError("extra_worker_ms is a simulated-worker stress input only")
    if not isinstance(selection, dict):
        raise ValueError("selection must declare an at time and normalized box")
    selected = deepcopy(selection)
    selected["at"] = _number(selected.get("at"), "selection.at")
    if not start <= selected["at"] <= end:
        raise ValueError("selection time must be within the replay window")
    selected["min_iou"] = _number(selected.get("min_iou", .5), "selection.min_iou", positive=True)
    if selected["min_iou"] > 1:
        raise ValueError("selection.min_iou must not exceed one")
    VisionService._validate_result(dict(width=1, height=1, inference_ms=0.,
        detections=[dict(box=selected.get("box"), confidence=1.)]))
    if not isinstance(frames, list) or not frames:
        raise ValueError("a nonempty frame list is required")
    cleaned, previous_at, previous_sequence, previous_available = [], None, None, None
    dimensions = None
    for incoming in frames:
        if not isinstance(incoming, dict):
            raise ValueError("each frame must be an object")
        frame = deepcopy(incoming)
        sequence = frame.get("sequence")
        if type(sequence) is not int or not 1 <= sequence <= 2**53 - 1:
            raise ValueError("frame sequence must be a positive safe integer")
        received = frame["received_at"] = _number(frame.get("received_at"), "frame.received_at")
        if (previous_at is not None and received <= previous_at
                or previous_sequence is not None and sequence <= previous_sequence):
            raise ValueError("frame receipt times and sequences must strictly increase")
        if not start <= received <= end:
            raise ValueError("all source frames must belong to the replay window")
        available = frame.get("available_at")
        if available is not None:
            available = frame["available_at"] = _number(available, "frame.available_at")
            if available < received:
                raise ValueError("result availability cannot precede image receipt")
            if previous_available is not None and available <= previous_available:
                raise ValueError("recorded result observation times must strictly increase")
            previous_available = available
        frame["worker_ms"] = _number(frame.get("worker_ms"), "frame.worker_ms")
        frame["inference_ms"] = _number(frame.get("inference_ms", 0.), "frame.inference_ms")
        result = VisionService._validate_result({key: frame.get(key) for key in
                                                ("width", "height", "inference_ms", "detections")})
        shape = result["width"], result["height"]
        if dimensions is not None and shape != dimensions:
            raise ValueError("one replay requires consistent image dimensions")
        dimensions = shape
        frame["detections"] = result["detections"]
        frame["appearances"] = validate_appearances(frame.get("appearances"), len(frame["detections"]))
        cleaned.append(frame)
        previous_at, previous_sequence = received, sequence
    return cleaned, start, end, selected, max_hz, tick_seconds, extra_worker_ms


def _preview_suppressions(entries, frames, selection):
    """Validate an exact, image-bound annotation intervention before replay.

    An image hash alone does not bind a detector index: expected box/confidence
    also protect against applying annotations to another detector's output.
    Review truth is supplied by the caller, not inferred by this harness.
    """
    if entries is None:
        return None
    if not isinstance(entries, list):
        raise ValueError("preview_suppressions must be a list of annotation records")
    by_sequence = {frame["sequence"]: frame for frame in frames}
    required = {"sequence", "frame_sha256", "detection_index", "expected_box",
                "expected_confidence", "reason", "evidence_id"}
    validated, seen = {}, set()
    for incoming in entries:
        if not isinstance(incoming, dict) or set(incoming) != required:
            raise ValueError("preview suppression requires the exact annotation record fields")
        record = deepcopy(incoming)
        sequence, index = record["sequence"], record["detection_index"]
        if type(sequence) is not int or sequence not in by_sequence:
            raise ValueError("preview suppression sequence must identify an exact source frame")
        frame = by_sequence[sequence]
        if type(index) is not int or not 0 <= index < len(frame["detections"]):
            raise ValueError("preview suppression index must identify an exact source detection")
        if (sequence, index) in seen:
            raise ValueError("duplicate preview suppression for one source detection")
        seen.add((sequence, index))
        sha = record["frame_sha256"]
        if (not isinstance(sha, str) or len(sha) != 64
                or any(character not in "0123456789abcdef" for character in sha)
                or frame.get("sha256") != sha):
            raise ValueError("preview suppression frame hash does not match the exact source image")
        if "jpeg" in frame:
            jpeg = frame["jpeg"]
            if not isinstance(jpeg, bytes) or hashlib.sha256(jpeg).hexdigest() != sha:
                raise ValueError("preview suppression source JPEG does not match its hash")
        expected = {"box": record["expected_box"], "confidence": record["expected_confidence"]}
        VisionService._validate_result(dict(width=frame["width"], height=frame["height"],
                                             inference_ms=0., detections=[expected]))
        if expected != frame["detections"][index]:
            raise ValueError("preview suppression expected box/confidence does not match its source index")
        if record["reason"] not in ("reviewed_duplicate", "reviewed_false_positive"):
            raise ValueError("preview suppression reason must identify a reviewed duplicate or false positive")
        evidence = record["evidence_id"]
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 512:
            raise ValueError("preview suppression evidence_id must contain 1..512 characters")
        if frame["received_at"] <= selection["at"]:
            raise ValueError("preview suppression cannot affect frames received before or at initial selection")
        validated.setdefault(sequence, []).append(record)
    for records in validated.values():
        records.sort(key=lambda record: record["detection_index"])
    return validated


class _Camera:
    def __init__(self, clock):
        self.clock, self.sample = clock, None

    def read_current(self):
        now = self.clock()
        sample = self.sample
        if sample is not None and not 0 <= now - sample.received_at <= 1.:
            sample = None
        return now, sample


class _Process:
    """Only the is_alive/terminate boundary is needed; no process is created."""

    alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False


class _ComparisonTracker:
    """Adapt an offline association result to the unchanged selection contract.

    Alternative trackers may hide unconfirmed detections or return predicted
    boxes. Selection must still see *every* current detector candidate, including
    a strong candidate which could make recovery ambiguous. Use only the native
    association IDs and their input indices; all other measurements come from the
    frozen detector input. Omitted rows receive unique display-only IDs.
    """

    def __init__(self, tracker, frames, image_provider):
        self.tracker = tracker
        self.frames = {frame["received_at"]: frame for frame in frames}
        self.image_provider = image_provider
        self.records = {}
        self.failure = None
        self._ephemeral_id = 2**53 - 1

    def reset(self):
        self.tracker.reset()

    def update(self, detections, captured_at, *, appearances):
        try:
            return self._update(detections, captured_at, appearances=appearances)
        except Exception as exc:
            # VisionService deliberately turns malformed production results
            # into an unavailable frame. A benchmark adapter failure must abort
            # the experiment instead of masquerading as a selected-target loss.
            sequence = self.frames.get(captured_at, {}).get("sequence", "unknown")
            self.failure = RuntimeError(f"offline comparison tracker failed at frame {sequence}: {exc}")
            raise self.failure from exc

    def _update(self, detections, captured_at, *, appearances):
        frame = self.frames[captured_at]
        image, image_wall, image_cpu = None, 0., 0.
        if self.image_provider is not None:
            wall, cpu = time.perf_counter(), time.process_time()
            image = self.image_provider(deepcopy(frame))
            image_wall = (time.perf_counter() - wall) * 1000
            image_cpu = (time.process_time() - cpu) * 1000
        wall, cpu = time.perf_counter(), time.process_time()
        output = self.tracker.update(deepcopy(detections), captured_at,
            appearances=deepcopy(appearances), image=image,
            width=frame["width"], height=frame["height"])
        tracker_wall = (time.perf_counter() - wall) * 1000
        tracker_cpu = (time.process_time() - cpu) * 1000
        indices = self.tracker.last_detection_indices
        if (not isinstance(output, list) or not isinstance(indices, list)
                or len(output) != len(indices)
                or any(type(index) is not int or not 0 <= index < len(detections) for index in indices)
                or len(set(indices)) != len(indices)):
            raise ValueError("offline tracker output must map uniquely to current detections")
        assigned, native_ids = {}, set()
        for index, item in zip(indices, output):
            identity = item.get("track_id") if isinstance(item, dict) else None
            if (type(identity) is not int or not 1 <= identity < 2**52
                    or identity in native_ids):
                raise ValueError("offline tracker IDs must be unique positive integers below 2**52")
            native_ids.add(identity)
            assigned[index] = identity
        unassigned = []
        observed = []
        for index, detection in enumerate(detections):
            identity = assigned.get(index)
            if identity is None:
                identity = self._ephemeral_id
                self._ephemeral_id -= 1
                unassigned.append({"detection_index": index, "track_id": identity})
            observed.append(dict(deepcopy(detection), track_id=identity))
        self.records[frame["sequence"]] = {
            "native_detection_indices": list(indices),
            "native_track_ids": [item["track_id"] for item in output],
            "ephemeral_detections": unassigned,
            "unassigned": deepcopy(getattr(self.tracker, "last_unassigned", [])),
            "native_output_confirmed": deepcopy(getattr(self.tracker, "last_output_confirmed", None)),
            "tracker_wall_ms": tracker_wall, "tracker_cpu_ms": tracker_cpu,
            "image_provider_wall_ms": image_wall, "image_provider_cpu_ms": image_cpu,
        }
        return observed


def _snapshot(now, camera, candidate, preview):
    _, sample = camera.read_current()
    received = None if sample is None else sample.received_at
    analyzed = None if candidate is None else candidate.sample.received_at
    return {
        "schema_version": 1, "at": now, "run_id": RUN_ID, "environment": "real",
        "reconnecting": None,
        "configuration": {"environment": "real", "video_source": "device", "video_endpoint": ENDPOINT},
        "video": {"source_id": VIDEO_ID, "source": "device", "endpoint": ENDPOINT,
                  "state": "recent" if sample is not None else "stale",
                  "received_at": received, "rx_age_s": None if received is None else now - received,
                  "age_limit_s": 1.},
        "vision": {"configured": True, "state": "recent" if candidate is not None else "stale",
                   "frame_age_s": None if analyzed is None else now - analyzed,
                   "age_limit_s": 1., "inference_ms": None if candidate is None else candidate.result["inference_ms"]},
        "yaw_preview": preview,
    }


def replay(frames, *, start, end, selection, mode="recorded", max_hz=10,
           tick_seconds=.01, extra_worker_ms=0, tracker_factory=None,
           image_provider=None, preview_suppressions=None):
    """Return scalar diagnostics, per-source-frame decisions and software durations.

    ``frames`` contain sequence/receipt, normalized detections, aligned appearance
    descriptors, dimensions, measured worker_ms and optional recorded available_at.
    The latter is the observation timestamp in the log, not an exact worker time.
    ``selection`` makes one controlled attempt (at, box, optional min_iou=.5).
    Overlap resolves a current local ID; historical numeric IDs are never restored.
    Source frames must cover only [start, end]. A fresh tracker starts at start.

    ``tracker_factory(on_diagnostic=callback)`` optionally supplies an offline
    comparison adapter with update/reset and aligned last_detection_indices.
    ``image_provider(frame)`` supplies pixels for an adapter that needs them.
    Adapter CPU/wall and image-provider costs are measured separately, but do
    not alter the synthetic scheduler clock or worker duration. They execute at
    the owner-side association boundary, not in the detector worker. Default
    replay remains the original ImageTracker path without benchmark overhead.

    ``preview_suppressions`` is an optional list of exact sequence/image-hash/
    source-detection annotation records. It removes only those measurements from
    the observation passed to YawPreview after association and after an accepted
    initial selection. Raw detections, tracker state, schedules and thresholds
    are unchanged. This annotation-assisted counterfactual is not a deployable
    policy; annotations and their identity judgments are external evidence.
    """
    frames, start, end, selection, max_hz, tick_seconds, extra_worker_ms = _inputs(
        frames, start, end, selection, mode, max_hz, tick_seconds, extra_worker_ms)
    if tracker_factory is not None and not callable(tracker_factory):
        raise ValueError("tracker_factory must be callable")
    if image_provider is not None and (tracker_factory is None or not callable(image_provider)):
        raise ValueError("image_provider requires a custom tracker and must be callable")
    suppressions = _preview_suppressions(preview_suppressions, frames, selection)
    now = start
    events, pending, decisions = [], [], {}
    counter = 0

    def schedule(at, kind, value=None):
        nonlocal counter
        if at <= end:
            counter += 1
            heapq.heappush(pending, (float(at), counter, kind, value))

    def association(record):
        events.append({"kind": "association", "at": now, **record})

    def recovery(record):
        events.append({"kind": "recovery", **record})

    comparison = None
    if tracker_factory is None:
        tracker = ImageTracker(on_diagnostic=association)
    else:
        adapter = tracker_factory(on_diagnostic=association)
        if not callable(getattr(adapter, "update", None)) or not callable(getattr(adapter, "reset", None)):
            raise ValueError("tracker_factory must return an update/reset adapter")
        comparison = tracker = _ComparisonTracker(adapter, frames, image_provider)
    preview = YawPreview(True, continuous=True, on_recovery=recovery)
    validator = YawValidator()
    camera = _Camera(lambda: now)
    session = SimpleNamespace(run_id=RUN_ID, video_source_id=VIDEO_ID, video=camera,
                              config=SimpleNamespace(video_age=1.), clock=lambda: now)
    service = None
    if mode == "simulated":
        service = VisionService(Path("offline-cache.onnx"), variant="nano", max_hz=max_hz,
                                wall_clock=lambda: now)
        service._process = _Process()
        service._incoming, service._outgoing = Queue(maxsize=1), Queue(maxsize=1)
        service._ready, service._started_at, service._tracker = True, start, tracker
    by_sequence = {frame["sequence"]: frame for frame in frames}
    for frame in frames:
        sequence = frame["sequence"]
        decisions[sequence] = {"sequence": sequence, "received_at": frame["received_at"],
            "recorded_observation_at": frame.get("available_at"), "worker_ms": frame["worker_ms"],
            "status": "not_replayed" if mode == "recorded" else "not_analyzed",
            "reason": ("not_in_recorded_observations" if mode == "recorded"
                                                   else "superseded_before_admission")}
        if suppressions is not None:
            requested = suppressions.get(sequence, [])
            decisions[sequence]["preview_suppression"] = {
                "status": "not_delivered" if requested else "not_requested",
                "requests": deepcopy(requested), "suppressed_detection_indices": []}
        schedule(frame["received_at"], "camera", frame)
        if mode == "recorded" and frame.get("available_at") is not None:
            if frame["available_at"] > end:
                decisions[sequence].update(status="outside_window", reason="recorded_observation_after_end")
            schedule(frame["available_at"], "recorded_result", frame)
    tick = 0
    while start + tick * tick_seconds <= end:
        schedule(start + tick * tick_seconds, "tick")
        tick += 1
    schedule(end, "tick")
    schedule(selection["at"], "selection")
    candidate = None
    last_accepted = None
    last_service_error = None
    last_phase = last_demand = None
    last_time, phase_durations, admitted_seconds = start, Counter(), 0.
    selection_result = None
    last_state = preview.state(start)
    last_consumer = {"valid": False, "reason": "selection_required", "value": 0}
    delivered_ages, worker_delays = [], []
    accepted_sequences = set()
    filtered_sequence = filtered_observation = None

    while pending:
        now = pending[0][0]
        batch = []
        while pending and pending[0][0] == now:
            batch.append(heapq.heappop(pending))
        # Integrate the preceding software state, never an assumed human identity.
        dt = now - last_time
        phase_durations[last_state["phase"]] += dt
        admitted_seconds += dt * bool(last_consumer["valid"])
        last_time = now
        for _, _, kind, frame in batch:
            if kind == "camera":
                camera.sample = VideoSample(str(frame["sequence"]).encode("ascii"),
                                            frame["sequence"], frame["received_at"])
        for _, _, kind, job in batch:
            if kind == "worker_result":
                identifier, frame = job
                service._outgoing.put_nowait(("result", (identifier,
                    {key: deepcopy(frame[key]) for key in ("width", "height", "inference_ms", "detections")},
                    list(frame["appearances"]))))
                decisions[frame["sequence"]]["worker_completed_at"] = now
        delivered = []
        has_selection = any(item[2] == "selection" for item in batch)
        servicing = any(item[2] in ("tick", "selection", "recorded_result") for item in batch)
        if not servicing:
            continue
        if mode == "recorded":
            for _, _, kind, frame in batch:
                if kind != "recorded_result":
                    continue
                decision = decisions[frame["sequence"]]
                _, sample = camera.read_current()
                if sample is None or now - frame["received_at"] > 1.:
                    decision.update(status="rejected", reason="stale_result_at_observation", delivered_at=now)
                    events.append({"kind": "result_rejected", "at": now, **decision})
                    continue
                result = {key: deepcopy(frame[key]) for key in ("width", "height", "inference_ms", "detections")}
                result["detections"] = tracker.update(result["detections"], frame["received_at"],
                                                      appearances=list(frame["appearances"]))
                candidate = AnalyzedFrame(VideoSample(b"offline", frame["sequence"], frame["received_at"]),
                                          (RUN_ID, VIDEO_ID), result, appearances=tuple(frame["appearances"]))
                delivered.append(frame["sequence"])
        else:
            previous_pending = service._pending
            service.tick(session)
            if comparison is not None:
                if comparison.failure is not None:
                    raise comparison.failure
                if service._error:
                    raise RuntimeError(f"offline comparison vision failed: {service._error}")
            if service._error and service._error != last_service_error:
                events.append({"kind": "vision_error", "at": now, "reason": service._error})
                last_service_error = service._error
            candidate = service.frame(session)
            if candidate is not None and candidate.sample.sequence != last_accepted:
                delivered.append(candidate.sample.sequence)
            if previous_pending is not None:
                sequence = previous_pending[1].sample.sequence
                if (sequence not in accepted_sequences and sequence not in delivered
                        and (service._pending is None or service._pending[0] != previous_pending[0])):
                    decisions[sequence].update(status="rejected", reason="stale_or_unavailable_result", delivered_at=now)
                    events.append({"kind": "result_rejected", "at": now, **decisions[sequence]})
            try:
                identifier, jpeg = service._incoming.get_nowait()
            except Empty:
                pass
            else:
                frame = by_sequence[int(jpeg.decode("ascii"))]
                due = now + (frame["worker_ms"] + extra_worker_ms) / 1000
                decisions[frame["sequence"]].update(status="submitted", reason="completion_after_window",
                    submitted_at=now, synthetic_worker_ms=frame["worker_ms"] + extra_worker_ms)
                schedule(due, "worker_result", (identifier, frame))
                events.append({"kind": "worker_submitted", "at": now, "sequence": frame["sequence"],
                               "received_at": frame["received_at"], "synthetic_completion_at": due})
        _, current_camera = camera.read_current()
        if candidate is not None and (now - candidate.sample.received_at > 1. or current_camera is None):
            candidate = None
        observation = None if candidate is None else {
            "run_id": RUN_ID, "video_id": VIDEO_ID, "sequence": candidate.sample.sequence,
            "received_at": candidate.sample.received_at, "width": candidate.result["width"],
            "height": candidate.result["height"], "detections": candidate.result["detections"],
            "appearances": list(candidate.appearances),
        }
        if suppressions is not None and observation is not None:
            sequence = candidate.sample.sequence
            if sequence != filtered_sequence:
                requested = suppressions.get(sequence, [])
                can_suppress = bool(selection_result and selection_result["accepted"])
                removed = {record["detection_index"] for record in requested} if can_suppress else set()
                retained = [index for index in range(len(observation["detections"])) if index not in removed]
                filtered_sequence = sequence
                filtered_observation = dict(observation,
                    detections=[deepcopy(observation["detections"][index]) for index in retained],
                    appearances=[observation["appearances"][index] for index in retained])
                status = ("applied" if removed else "selection_not_accepted" if requested else "not_requested")
                info = decisions[sequence]["preview_suppression"]
                info.update(status=status, suppressed_detection_indices=sorted(removed))
                decisions[sequence].update(preview_detection_indices=retained,
                    preview_detections=deepcopy(filtered_observation["detections"]),
                    preview_appearance_available=[value is not None for value in filtered_observation["appearances"]])
                if requested:
                    events.append({"kind": "preview_suppression", "at": now, "sequence": sequence,
                                   "counterfactual": "annotation_assisted", **deepcopy(info)})
            # Repeated owner ticks must supply the identical observation for a
            # sequence, including a frame first delivered before selection.
            observation = filtered_observation
        # New results arrive before expiry evaluation at this same instant.
        preview.observe(observation, now)
        for sequence in delivered:
            accepted_sequences.add(sequence)
            last_accepted = sequence
            decision = decisions[sequence]
            age = now - by_sequence[sequence]["received_at"]
            delivered_ages.append(age)
            if "worker_completed_at" in decision:
                worker_delays.append(now - decision["worker_completed_at"])
            decision.update(status="accepted", reason="analyzed", delivered_at=now, image_age_s=age,
                detections=deepcopy(candidate.result["detections"]),
                appearance_available=[item is not None for item in candidate.appearances])
            if comparison is not None:
                decision["association"] = deepcopy(comparison.records[sequence])
        if has_selection:
            matches = [] if observation is None else [(item, _iou(selection["box"], item["box"]))
                for item in observation["detections"] if item["confidence"] >= .5]
            matches = [(item, overlap) for item, overlap in matches if overlap >= selection["min_iou"]]
            selection_result = {"at": now, "box": selection["box"], "min_iou": selection["min_iou"],
                                "identity_status": "human_unvalidated", "accepted": False}
            try:
                if len(matches) != 1:
                    raise RuntimeError("Selection needs exactly one strong detection overlapping the supplied box")
                item, overlap = matches[0]
                preview.select(item["track_id"], revision=preview.revision, now=now)
                selection_result.update(accepted=True, track_id=item["track_id"], iou=overlap,
                                        frame_sequence=candidate.sample.sequence)
            except RuntimeError as exc:
                selection_result["reason"] = str(exc)
            events.append({"kind": "selection", **selection_result})
        last_state = preview.state(now)
        try:
            trial = copy(validator)
            demand = trial.validate(_snapshot(now, camera, candidate, last_state), now, now,
                                    explicit_selection=bool(has_selection and selection_result["accepted"]))
            validator = trial
            last_consumer = asdict(demand)
        except PreviewError as exc:
            last_consumer = {"valid": False, "value": 0, "reason": "validator_rejected", "detail": str(exc)}
        phase_key = (last_state["phase"], last_state["target_id"], last_state["selection_epoch"], last_state["detail"])
        if phase_key != last_phase:
            events.append({"kind": "preview_transition", "at": now, **deepcopy(last_state)})
            last_phase = phase_key
        demand_key = (last_consumer["valid"], last_consumer["reason"], last_consumer.get("detail"),
                      tuple(last_consumer.get("selection_key") or ()))
        if demand_key != last_demand:
            events.append({"kind": "consumer_transition", "at": now, **deepcopy(last_consumer)})
            last_demand = demand_key
        for sequence in delivered:
            decisions[sequence].update(preview=deepcopy(last_state), consumer=deepcopy(last_consumer))
            events.append({"kind": "frame_analyzed", "at": now, "sequence": sequence,
                           "received_at": by_sequence[sequence]["received_at"],
                           "preview_phase": last_state["phase"], "consumer_valid": last_consumer["valid"]})

    def distribution(values):
        if not values:
            return {"count": 0, "p50": None, "p95": None, "max": None}
        ordered = sorted(values)
        return {"count": len(ordered), "p50": ordered[math.ceil(len(ordered) * .5) - 1],
                "p95": ordered[math.ceil(len(ordered) * .95) - 1], "max": ordered[-1]}

    report = {"events": events, "frame_decisions": list(decisions.values()), "summary": {
        "mode": mode, "start": start, "end": end, "tick_seconds": tick_seconds, "max_hz": max_hz,
        "extra_worker_ms": extra_worker_ms, "selection": selection_result,
        "source_frames": len(frames), "accepted_frames": len(accepted_sequences),
        "frame_status_counts": dict(Counter(item["status"] for item in decisions.values())),
        "phase_seconds": dict(phase_durations), "dry_consumer_valid_seconds": admitted_seconds,
        "delivered_image_age_s": distribution(delivered_ages),
        "synthetic_completion_to_collection_s": distribution(worker_delays),
        "final_preview": last_state, "final_consumer": last_consumer,
        "identity_status": "human_unvalidated", "historical_parity": False,
        "clock_basis": ("recorded observation timestamps are upper-bound availability proxies; regular intervening ticks are synthetic"
                        if mode == "recorded" else
                        "synthetic regular ticks, real latest-image scheduler and cached measured worker duration plus declared stress"),
        "limits": ["Controlled single selection and fresh tracker at window start; no historical internal state restored.",
                   "Dry software admission only; no radio, aircraft, native priority or physical response is modeled.",
                   "Receipt age is not camera exposure latency; software tracking is not verified person identity.",
                   "Frames missing from the vision log are omitted in recorded mode; this does not prove they were skipped live.",
                   "Snapshot validation uses zero local read overhead; worker cache timing is not a measured live workload."],
    }}
    if comparison is not None:
        report["summary"]["tracker"] = deepcopy(getattr(comparison.tracker, "metadata", {}))
        report["summary"]["association_cost"] = {
            key: distribution([record[key] for record in comparison.records.values()])
            for key in ("tracker_wall_ms", "tracker_cpu_ms", "image_provider_wall_ms", "image_provider_cpu_ms")}
        report["summary"]["limits"].extend([
            "Tracker and optional image-provider costs are measured on the owner side; they do not delay this synthetic clock.",
            ("All current detector candidates remain visible to selection; unassigned boxes have one-image ephemeral IDs."
             if suppressions is None else
             "All current detector candidates remain in raw association output; only explicit annotation counterfactuals may suppress selection inputs."),
            "Native association indices supply IDs only; selection uses original detector boxes, confidence and appearance.",
        ])
    if suppressions is not None:
        ordered = [record for sequence in sorted(suppressions) for record in suppressions[sequence]]
        statuses = [decision["preview_suppression"] for decision in decisions.values()
                    if decision["preview_suppression"]["requests"]]
        report["summary"]["preview_counterfactual"] = {
            "kind": "annotation_assisted_suppression", "deployable_policy": False,
            "manifest_sha256": hashlib.sha256(json.dumps(ordered, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "requested_detections": len(ordered),
            "suppressed_detections": sum(len(item["suppressed_detection_indices"]) for item in statuses),
            "requested_frame_status_counts": dict(Counter(item["status"] for item in statuses)),
        }
        report["summary"]["limits"].extend([
            "Annotation-assisted counterfactual, not a deployable policy; only exact reviewed detection indices are suppressed before selection recovery.",
            "Annotations do not alter association, source receipts, worker schedules, thresholds or initial selection; unknown and unannotated candidates remain.",
            "Avoiding one ambiguity stop does not establish recovery or correct identity later in the window.",
        ])
    return report
