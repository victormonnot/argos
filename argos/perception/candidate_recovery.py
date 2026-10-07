"""Offline multi-candidate qualification without removing raw detections.

This is an experiment in recovery, not a real-world identity guarantee. A strong
currently selected track continues unchanged, including the known limitation
that an upstream ID switch can be invisible. Once paused, even the old numeric
ID needs compatible appearance/geometry on two fresh images during a recovery
episode involving multiple strong candidates. Outside such an episode the
held-motion baseline is unchanged. Similar-looking people and confidently wrong
descriptors remain explicit counterexamples.
"""
from __future__ import annotations

from argos.console.yaw_preview import (
    CONTINUOUS_RECOVERY_MAX_DX, CONTINUOUS_RECOVERY_MAX_DY,
    CONTINUOUS_RECOVERY_MIN_SIMILARITY, FRAME_MAX_AGE, MIN_CONFIDENCE,
)
from .recovery_prototype import RecoveryPrototype


# Fixed before the experiment, not fitted or calibrated identity uncertainty.
CANDIDATE_SIMILARITY_MARGIN = .05


class CandidateRecoveryPrototype(RecoveryPrototype):
    """Qualify all strong recovery candidates, waiting on unresolved ambiguity."""

    def __init__(self, *, on_recovery=None, on_policy=None):
        super().__init__("motion_held_candidates", on_recovery=on_recovery,
                         on_policy=on_policy)
        self._qualification_frame = None
        self._competition_latched = False

    @property
    def metadata(self):
        return {**super().metadata, "version": 3,
                "candidate_qualification": "unique_compatible_with_plausible_competitor_margin",
                "candidate_similarity_margin": CANDIDATE_SIMILARITY_MARGIN,
                "candidate_margin_kind": "fixed_assumption_not_calibrated",
                "candidate_scope": "lost_or_paused_multi_strong_episode",
                "ambiguous_recovery": "pause_until_existing_deadline",
                "paused_same_id": "full_gates_and_two_fresh_images_during_competition_episode",
                "active_same_id": "unchanged_upstream_identity_limitation",
                "duplicate_filter": False,
                "production_recovery_gates": "unchanged_except_horizontal_reference_and_candidate_qualification"}

    def _reset_motion(self):
        super()._reset_motion()
        self._competition_latched = False

    def _update_preview(self, now):
        # Delegate inactive/invalid/expired lifecycle transitions verbatim.
        frame = self._frame
        if (self.phase not in ("tracking", "paused") or frame is None
                or not 0 <= now - frame["received_at"] <= FRAME_MAX_AGE
                or now >= self._last_seen_at + self.recovery_max_gap):
            return super()._update_preview(now)
        current = next((item for item in frame["detections"]
                        if item["track_id"] == self._target_id), None)
        if (self.phase == "tracking" and current is not None
                and current["confidence"] >= MIN_CONFIDENCE):
            return super()._update_preview(now)

        if not self._competition_latched:
            strong = [item for item in frame["detections"]
                      if item["confidence"] >= MIN_CONFIDENCE]
            if len(strong) <= 1:
                return super()._update_preview(now)
            # A new competition episode cannot inherit a confirmation image
            # that was qualified under the old single-candidate rule.
            self._competition_latched = True
            self._recovery_candidate = None

        if self.phase != "paused":
            self.revision += 1
        self.phase = "paused"
        self._detail = "Person briefly obscured or uncertain; preview paused with zero correction"
        self._error_x = None
        self._yaw = 0.
        target = self._recover_qualified(now)
        if target is None:
            return

        # Qualification has supplied the two-image evidence. The unchanged base
        # update computes command geometry and renews reference/evidence only
        # from this current measured target. Mark it active before delegating so
        # the old count-only paused guard cannot override the new qualification.
        self._target_id = target["track_id"]
        self.phase = "tracking"
        self._competition_latched = False
        super()._update_preview(now)

    def _candidate_row(self, index, target, now):
        metrics = self._recovery_metrics(target, now)
        strong = target["confidence"] >= MIN_CONFIDENCE
        geometry = bool(strong and self._reference is not None
            and .5 <= metrics["width_ratio"] <= 2
            and .75 <= metrics["height_ratio"] <= 4 / 3
            and metrics["dx"] <= CONTINUOUS_RECOVERY_MAX_DX + 1e-12
            and metrics["dy"] <= CONTINUOUS_RECOVERY_MAX_DY + 1e-12)
        reason = None
        if not strong:
            reason = "low_confidence"
        elif self._reference is None:
            reason = "no_reference"
        elif metrics["similarity"] is None:
            reason = "appearance_unavailable"
        elif not 0 <= metrics["reference_appearance_age_s"] < self.recovery_max_gap:
            reason = "reference_appearance_expired"
        elif metrics["similarity"] < CONTINUOUS_RECOVERY_MIN_SIMILARITY:
            reason = "appearance_similarity"
        elif not .5 <= metrics["width_ratio"] <= 2:
            reason = "width_ratio"
        elif not .75 <= metrics["height_ratio"] <= 4 / 3:
            reason = "height_ratio"
        elif metrics["dx"] > CONTINUOUS_RECOVERY_MAX_DX + 1e-12:
            reason = "horizontal_distance"
        elif metrics["dy"] > CONTINUOUS_RECOVERY_MAX_DY + 1e-12:
            reason = "vertical_distance"
        elif self._frame["received_at"] <= self._last_seen_at:
            reason = "old_image_receipt"
        return dict(detection_index=index, track_id=target["track_id"],
                    confidence=target["confidence"], strong=strong,
                    geometry_plausible=geometry, eligible=reason is None,
                    reason=reason or "compatible", **metrics)

    @staticmethod
    def _qualified_winner(rows):
        eligible = [row for row in rows if row["eligible"]]
        if not eligible:
            return None, "no_eligible_candidate"
        if len(eligible) > 1:
            return None, "multiple_eligible_candidates"
        winner = eligible[0]
        competitors = [row for row in rows if row is not winner and row["geometry_plausible"]]
        if any(row["similarity"] is None for row in competitors):
            return None, "unknown_plausible_competitor"
        if any(winner["similarity"] - row["similarity"]
               < CANDIDATE_SIMILARITY_MARGIN - 1e-12 for row in competitors):
            return None, "appearance_margin"
        return winner, "unique_compatible_candidate"

    def _recover_qualified(self, now):
        frame = self._frame
        rows = [self._candidate_row(i, target, now)
                for i, target in enumerate(frame["detections"])]
        winner, reason = self._qualified_winner(rows)
        accepted = False
        decision = "refused"
        target = None if winner is None else frame["detections"][winner["detection_index"]]
        if winner is None:
            self._recovery_candidate = None
        else:
            candidate = (target["track_id"], frame["sequence"], frame["received_at"])
            previous = self._recovery_candidate
            accepted = bool(previous is not None and previous[0] == candidate[0]
                            and candidate[1] > previous[1] and candidate[2] > previous[2])
            decision = "accepted_appearance" if accepted else "pending_second_image"
            if previous is None or previous[0] != candidate[0]:
                self._recovery_candidate = candidate

        for row, candidate in zip(rows, frame["detections"]):
            is_winner = row is winner
            row_reason = decision if is_winner else (reason if row["eligible"] else row["reason"])
            self._emit_recovery(candidate, now, accepted and is_winner, row_reason,
                                {key: value for key, value in row.items()
                                 if key not in ("detection_index", "track_id", "confidence",
                                                "strong", "geometry_plausible", "eligible", "reason")})
        if not rows:
            self._emit_recovery(None, now, False, "no_candidate")
        self._emit_qualification(now, rows, winner, reason, decision)
        return target if accepted else None

    def _emit_qualification(self, now, rows, winner, reason, decision):
        frame = self._frame
        key = (frame["context"], frame["sequence"], self.selection_epoch)
        if key == self._qualification_frame:
            return
        self._qualification_frame = key
        if self._on_policy is None:
            return
        event = dict(event="candidate_qualification", policy=self.policy, at=now,
                     run_id=frame["context"][0], video_id=frame["context"][1],
                     selection_epoch=self.selection_epoch,
                     frame_sequence=frame["sequence"], frame_received_at=frame["received_at"],
                     reason=reason, decision=decision, rows=rows,
                     winner_track_id=None if winner is None else winner["track_id"],
                     winner_detection_index=None if winner is None else winner["detection_index"],
                     pending_track_id=(None if self._recovery_candidate is None else
                                       self._recovery_candidate[0]))
        try:
            self._on_policy(event)
        except Exception:
            # A diagnostic sink cannot grant or withdraw target authority.
            pass
