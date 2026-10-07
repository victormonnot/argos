"""Offline-only selected-person recovery experiments.

No production owner imports this module. Motion changes only the horizontal
recovery comparison; commands always come from a current measured box. The
optional duplicate rule uses current geometry/appearance, never annotations.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from itertools import combinations

from argos.console.yaw_preview import FRAME_MAX_AGE, MIN_CONFIDENCE, YawPreview
from .appearance import similarity
from .image_tracks import _iou


POLICIES = ("motion", "motion_duplicates", "motion_held", "motion_held_duplicates",
            "motion_held_candidates")
HELD_POLICIES = ("motion_held", "motion_held_duplicates", "motion_held_candidates")
DUPLICATE_POLICIES = ("motion_duplicates", "motion_held_duplicates")
HISTORY_MAX_AGE_S = .7
PREDICTION_MAX_HORIZON_S = .5
VELOCITY_MAX_X_PER_S = 1.
# A conservative fixed allowance, not a fitted or statistical error bound.
PREDICTION_UNCERTAINTY_X_PER_S = .1
DUPLICATE_MIN_IOU = .85
DUPLICATE_MIN_SIMILARITY = .95


@dataclass(frozen=True)
class _SelectedSample:
    sequence: int
    received_at: float
    cx: float


@dataclass(frozen=True)
class _HeldMotion:
    anchor: _SelectedSample
    previous_sequence: int
    previous_received_at: float
    velocity_x: float


class RecoveryPrototype(YawPreview):
    """Continuous preview with fixed, isolated recovery-policy ablations.

    A descriptor and its geometry stay paired in the production reference.
    Separate motion samples require an actually selected, strong measurement;
    candidate-only and repeated owner reads never add evidence. Both source
    sequence and receipt must advance before another sample is retained.

    Duplicate filtering is temporary inside the decision, so the production
    observation validator continues to see the complete, unmodified raw frame.
    The chosen source indices are immutable for that frame and explicit
    selection, including polls. An explicit clear/reselection starts afresh.
    """

    def __init__(self, policy, *, on_recovery=None, on_policy=None):
        if policy not in POLICIES:
            raise ValueError(f"unknown offline recovery policy: {policy!r}")
        if on_policy is not None and not callable(on_policy):
            raise ValueError("Policy diagnostics require a callable")
        super().__init__(True, continuous=True, on_recovery=on_recovery)
        self.policy = policy
        self._on_policy = on_policy
        self._motion_history = []
        self._held_last_selected = None
        self._held_motion = None
        self._motion_epoch = self.selection_epoch
        self._policy_frame = None
        self._policy_indices = []
        self._policy_detections = []

    @property
    def metadata(self):
        metadata = dict(policy=self.policy, offline_only=True, version=1,
                    history_max_age_s=HISTORY_MAX_AGE_S,
                    prediction_max_horizon_s=PREDICTION_MAX_HORIZON_S,
                    velocity_max_x_per_s=VELOCITY_MAX_X_PER_S,
                    duplicate_min_iou=DUPLICATE_MIN_IOU,
                    duplicate_min_similarity=DUPLICATE_MIN_SIMILARITY,
                    duplicate_filter=self.policy in DUPLICATE_POLICIES,
                    duplicate_priority="selected_id,pending_id,confidence,source_index",
                    command_geometry="current_measurement_only",
                    production_recovery_gates="unchanged_except_horizontal_reference")
        if self.policy in HELD_POLICIES:
            metadata.update(version=2, motion_estimate="held_selected_velocity",
                estimate_history_age_at="anchor_receipt",
                prediction_uncertainty_x_per_s=PREDICTION_UNCERTAINTY_X_PER_S,
                prediction_uncertainty_kind="fixed_assumption_not_calibrated",
                prediction_geometry_gate="absolute_residual_plus_margin")
        return metadata

    @property
    def preview_detection_indices(self):
        return list(self._policy_indices)

    @property
    def preview_detections(self):
        """Current effective measured detections, with no appearance vectors."""
        return deepcopy(self._policy_detections)

    def _reset_motion(self):
        self._motion_history.clear()
        self._held_last_selected = None
        self._held_motion = None
        self._motion_epoch = self.selection_epoch

    def _stop(self, detail):
        super()._stop(detail)
        self._reset_motion()

    def clear(self, reason="Preview cleared"):
        super().clear(reason)
        self._reset_motion()
        self._policy_frame = None

    def _duplicate_indices(self, frame, now):
        indices = list(range(len(frame["detections"])))
        if self.policy not in DUPLICATE_POLICIES:
            return indices, "disabled"
        if self.phase not in ("tracking", "paused"):
            return indices, "inactive_selection"
        if not 0 <= now - frame["received_at"] <= FRAME_MAX_AGE:
            return indices, "stale_image"
        if now >= self._last_seen_at + self.recovery_max_gap:
            return indices, "recovery_deadline"
        strong = [i for i, item in enumerate(frame["detections"])
                  if item["confidence"] >= MIN_CONFIDENCE]
        selected = next((i for i in strong
                         if frame["detections"][i]["track_id"] == self._target_id), None)
        if self.phase != "paused" and selected is not None:
            return indices, "selected_id_present"
        if len(strong) < 2:
            return indices, "fewer_than_two_strong_candidates"
        # Require every pair, not a transitive chain or overlap with one anchor.
        for left, right in combinations(strong, 2):
            a, b = frame["detections"][left], frame["detections"][right]
            if _iou(a["box"], b["box"]) < DUPLICATE_MIN_IOU:
                return indices, "separate_geometry"
            score = similarity(a.get("appearance"), b.get("appearance"))
            if score is None:
                return indices, "appearance_unavailable"
            if score < DUPLICATE_MIN_SIMILARITY:
                return indices, "different_appearance"
        pending = None if self._recovery_candidate is None else self._recovery_candidate[0]
        keep = min(strong, key=lambda i: (
            frame["detections"][i]["track_id"] != self._target_id,
            frame["detections"][i]["track_id"] != pending,
            -frame["detections"][i]["confidence"], i))
        return [i for i in indices if i not in strong or i == keep], "strict_duplicate_clique"

    def _effective_frame(self, frame, now):
        key = (frame["context"], frame["dimensions"], frame["sequence"], frame["received_at"])
        if key != self._policy_frame:
            retained, reason = self._duplicate_indices(frame, now)
            self._policy_frame = key
            self._policy_indices = retained
            self._policy_detections = [
                {"track_id": frame["detections"][i]["track_id"],
                 "box": list(frame["detections"][i]["box"]),
                 "confidence": frame["detections"][i]["confidence"]}
                for i in retained]
            event = dict(event="recovery_policy", policy=self.policy, at=now,
                         run_id=frame["context"][0], video_id=frame["context"][1],
                         selection_epoch=self.selection_epoch,
                         frame_sequence=frame["sequence"],
                         frame_received_at=frame["received_at"],
                         retained_detection_indices=list(retained),
                         removed_detection_indices=[i for i in range(len(frame["detections"]))
                                                    if i not in retained],
                         duplicate_reason=reason)
            if self._on_policy is not None:
                try:
                    self._on_policy(event)
                except Exception:
                    # A diagnostic sink cannot change selected-target authority.
                    pass
        return {**frame, "detections": [frame["detections"][i]
                                        for i in self._policy_indices]}

    def _update(self, now):
        if self._motion_epoch != self.selection_epoch:
            self._reset_motion()
            # An explicit selection may choose a box suppressed by the prior
            # selection. Raw image identity remains separately validated.
            self._policy_frame = None
        frame = self._frame
        if frame is None:
            self._policy_indices = []
            self._policy_detections = []
            return self._update_preview(now)
        self._motion_history = [sample for sample in self._motion_history
                                if 0 <= frame["received_at"] - sample["received_at"]
                                <= HISTORY_MAX_AGE_S]
        effective = self._effective_frame(frame, now)
        self._frame = effective
        try:
            self._update_preview(now)
            if self.phase == "tracking":
                target = next(item for item in effective["detections"]
                              if item["track_id"] == self._target_id)
                previous = self._motion_history[-1] if self._motion_history else None
                if (target["confidence"] >= MIN_CONFIDENCE
                        and (previous is None or
                             (frame["sequence"] > previous["sequence"]
                              and frame["received_at"] > previous["received_at"]))):
                    x, _, width, _ = target["box"]
                    self._motion_history.append(dict(sequence=frame["sequence"],
                        received_at=frame["received_at"], cx=x + width / 2))
                if self.policy in HELD_POLICIES and target["confidence"] >= MIN_CONFIDENCE:
                    self._remember_selected_motion(frame, target)
        finally:
            self._frame = frame

    def _update_preview(self, now):
        """Keep legacy lifecycle behavior separate from offline experiments."""
        return super()._update(now)

    def _remember_selected_motion(self, frame, target):
        """Freeze velocity at a selected measurement, before history can age out.

        Rejected/paused candidates never call this method. Retaining the last
        selected receipt separately also prevents polls or equal-time frames
        from manufacturing fresh evidence after the rolling history is pruned.
        """
        previous = self._held_last_selected
        if previous is not None and not (
                frame["sequence"] > previous.sequence
                and frame["received_at"] > previous.received_at):
            return
        x, _, width, _ = target["box"]
        anchor = _SelectedSample(frame["sequence"], frame["received_at"], x + width / 2)
        self._held_motion = None
        if previous is not None and anchor.received_at - previous.received_at <= HISTORY_MAX_AGE_S:
            velocity = (anchor.cx - previous.cx) / (anchor.received_at - previous.received_at)
            self._held_motion = _HeldMotion(anchor, previous.sequence, previous.received_at,
                max(-VELOCITY_MAX_X_PER_S, min(VELOCITY_MAX_X_PER_S, velocity)))
        self._held_last_selected = anchor

    def _recovery_metrics(self, target, now):
        metrics = super()._recovery_metrics(target, now)
        metrics.update(static_dx=metrics["dx"], motion_applied=False,
                       motion_reason="insufficient_selected_history",
                       motion_anchor_sequence=None, motion_anchor_received_at=None,
                       motion_velocity_x=None, motion_horizon_s=None,
                       motion_predicted_cx=None)
        if self.policy in HELD_POLICIES:
            return self._held_recovery_metrics(target, metrics)
        if target is None or self._reference is None or self._frame is None:
            metrics["motion_reason"] = "missing_candidate_or_reference"
            return metrics
        if len(self._motion_history) < 2:
            return metrics
        previous, anchor = self._motion_history[-2:]
        horizon = self._frame["received_at"] - anchor["received_at"]
        velocity = max(-VELOCITY_MAX_X_PER_S, min(VELOCITY_MAX_X_PER_S,
                       (anchor["cx"] - previous["cx"]) /
                       (anchor["received_at"] - previous["received_at"])))
        metrics.update(motion_anchor_sequence=anchor["sequence"],
                       motion_anchor_received_at=anchor["received_at"],
                       motion_velocity_x=velocity, motion_horizon_s=horizon)
        if horizon <= 0 or self._frame["sequence"] <= anchor["sequence"]:
            metrics["motion_reason"] = "candidate_not_newer_than_anchor"
            return metrics
        if horizon > PREDICTION_MAX_HORIZON_S:
            metrics["motion_reason"] = "prediction_horizon_expired"
            return metrics
        predicted = anchor["cx"] + velocity * horizon
        x, _, width, _ = target["box"]
        metrics.update(dx=abs(x + width / 2 - predicted), motion_applied=True,
                       motion_reason="bounded_horizontal_prediction",
                       motion_predicted_cx=predicted)
        return metrics

    def _held_recovery_metrics(self, target, metrics):
        metrics.update(motion_previous_sequence=None, motion_previous_received_at=None,
                       motion_anchor_cx=None, motion_residual_x=None,
                       motion_uncertainty_margin_x=None, motion_effective_dx=metrics["dx"])
        if target is None or self._reference is None or self._frame is None:
            metrics["motion_reason"] = "missing_candidate_or_reference"
            return metrics
        estimate = self._held_motion
        if estimate is None:
            return metrics
        anchor = estimate.anchor
        horizon = self._frame["received_at"] - anchor.received_at
        metrics.update(motion_anchor_sequence=anchor.sequence,
                       motion_anchor_received_at=anchor.received_at,
                       motion_anchor_cx=anchor.cx,
                       motion_previous_sequence=estimate.previous_sequence,
                       motion_previous_received_at=estimate.previous_received_at,
                       motion_velocity_x=estimate.velocity_x, motion_horizon_s=horizon)
        if horizon <= 0 or self._frame["sequence"] <= anchor.sequence:
            metrics["motion_reason"] = "candidate_not_newer_than_anchor"
            return metrics
        if horizon > PREDICTION_MAX_HORIZON_S:
            metrics["motion_reason"] = "prediction_horizon_expired"
            return metrics
        predicted = anchor.cx + estimate.velocity_x * horizon
        x, _, width, _ = target["box"]
        residual = abs(x + width / 2 - predicted)
        margin = PREDICTION_UNCERTAINTY_X_PER_S * horizon
        effective_dx = residual + margin
        metrics.update(dx=effective_dx, motion_applied=True,
                       motion_reason="bounded_held_horizontal_prediction",
                       motion_predicted_cx=predicted, motion_residual_x=residual,
                       motion_uncertainty_margin_x=margin, motion_effective_dx=effective_dx)
        return metrics


def make_preview(policy, *, on_recovery=None, on_policy=None):
    """Construct a fixed policy; importing it never installs a runtime hook."""
    if policy == "motion_held_candidates":
        from .candidate_recovery import CandidateRecoveryPrototype
        return CandidateRecoveryPrototype(on_recovery=on_recovery, on_policy=on_policy)
    return RecoveryPrototype(policy, on_recovery=on_recovery, on_policy=on_policy)
