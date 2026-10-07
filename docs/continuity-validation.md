# Dense review and recovery counterfactuals

`examples/validate_continuity.py` extends the [offline tracker comparison](tracker-comparison.md)
with a reviewed short span. It reuses the original inference cache, prehistory,
selection, association parameters and clocks. No new inference, device, active
configuration or production policy is involved.

```sh
dist/tracking-venv/bin/python examples/validate_continuity.py \
  --source-comparison /path/to/completed-comparison \
  --reference /path/to/reference.json \
  --candidate-review /path/to/detection-review.json \
  --output-dir /path/to/new-validation
```

Use the preceding comparison's isolated environment. Output must be new or empty
and outside that comparison. Evidence, implementation and original JPEG hashes
are checked before/after execution. Default replays must reproduce the saved
outcomes, excluding execution costs. ImageTracker and ByteTrack run under both
clocks; every condition repeats twice to check semantic reproducibility.

## Two annotation passes

Inspect every original image in the declared span first, drawing approximate
visible-person extent and recording clipping, uncertainty and other people.
Freeze those coordinates before overlaying detector predictions. Do not invent
extent behind an occluder or accept unreviewed interpolation. Then classify each
cached detection. Disclose knowledge of earlier results; this is not a blinded
review. Sparse user confirmations do not validate dense coordinates or all
intervening identities.

The current contract accepts only explicit **assistant review**, with a named
`reviewer`, `review_status: "assistant_reviewed"` and `human_validated: false`.
It must never silently promote these observations to human ground truth.

`argos.continuity.dense-reference` version 1 contains those review fields,
`window`, `start`, `end`, and `frames` covering **every source image** in the span:

| Field | Meaning |
| --- | --- |
| `sequence`, `sha256`, `received_at` | Exact original image and receipt-clock binding |
| `review_status` | `assistant_reviewed` |
| `target.identity` | Clip-local `person-1`, separate from tracker IDs |
| `target.visibility` | `visible`, `partial`, `occluded`, `offscreen`, `uncertain` |
| `target.spatial_quality` | `clear` or `uncertain` |
| `target.box` | Approximate visible extent, normalized xywh; null if unknown |
| `other_people` | Reviewed `{identity, box}` entries; empty means none found, null means unknown |

`argos.continuity.detection-review` version 1 binds `reference_sha256` and
`cache_sha256`, with the same explicit reviewer/status/validation fields. Every
reference image, including empty detections, has `sequence`, `frame_sha256` and
`detections`. Candidate entries bind `detection_index`, original `box` and
`confidence`, then add `classification` (`target`, `other_person`, `not_person`,
`unknown`), `person_id` and nonempty `evidence_id`. Target means `person-1`; other
people must exist in the spatial reference; nonperson/unknown have null identity.
Missing, duplicated or changed source candidates fail validation.

Spatial overlap and identity class are different observations. A small box on
the target's upper body may be recognizably the target while overlapping less
than half its whole visible extent. IoU is reported separately.

## Selection-only interventions

Four fixed conditions isolate candidate input to recovery:

- `unchanged`: full output, exactly the preceding comparison.
- `duplicates_only`: retain the highest-confidence reviewed target detection;
  the smallest source index breaks ties. Remove other target rows.
- `false_positives_only`: remove only explicitly reviewed `not_person` rows.
- `both`: apply those two rules together.

Every detection still enters the tracker. Removal occurs **after association**,
only in a copied observation passed to `YawPreview`, after the original accepted
selection. Raw measurements, native IDs, worker submissions/delivery times,
image ages, thresholds, recovery deadline and two-image confirmation remain
unchanged. Appearances use the same retained indices. Unknowns and actual other
people always remain, even when their boxes overlap the target. No predicted or
replacement observation is introduced.

`replay(..., preview_suppressions=...)` takes explicit records bound to sequence,
JPEG hash, index, expected box/score, review reason and evidence ID. Preselection
requests and mismatches fail. An omitted source image never transfers its
annotation to a nearby image. Repeated polls retain identical filtered content.
A failed initial selection disables the intervention. Raw `detections` remain;
`preview_detections`, `preview_detection_indices` and `preview_suppression`
separately describe selection input and application status. The default `None`
path preserves prior output.

These are **annotation-assisted counterfactuals**, not deployable deduplication
policies or numerical upper bounds on quality. The fixed survivor can remove
the selected ID or change recovery geometry/appearance; outcomes can worsen.
Report such removals. Avoiding immediate ambiguity may only postpone failure
to a deadline or leave selection paused at window end. Stops remain latched.
Deadline expiry may appear in a state transition without a new deduplicated
recovery event.

## Results and limits

`report.json` retains every condition, raw/filtered measurements, hashes,
repeatability and upstream parity. `index.html` shows original images and
reviewed extent beside unchanged/filtered results, marking removed candidates.
Unanalyzed images never show inherited tracker boxes as fresh observations.

Counts separate source coverage, admitted images and admitted images with clear
visible references. Fresh selection is classified against candidate review,
including other-person, nonperson and unknown outcomes. No reviewed target,
only weak target and strong target left unselected are separate. Unresolved
labels cannot inflate detector-miss counts. Uncertain extents/skipped frames
remain outside the eligible denominator; no nearest-frame substitution occurs.
Phase/dry-admission durations are clipped to the reviewed span and remain
software measurements, not physical correct-person tracking time.

A previously examined single-person passage cannot qualify identity safety in
crowds or establish a global wrong-person rate. Broader claims require independent
recordings and appropriate human review. If available native takes all belong
to IRIS-reserved scenes, a nearby interval is not independent validation.

The preceding desktop CPU/RSS comparison retains its stated scope. Annotation
lookup/filter execution is not a deployable performance result; the simulated
clock still omits owner blocking and IPC. Live integration, further policy
experiments and laptop measurements are separate work.

The [offline recovery prototype](recovery-prototype.md) tests bounded horizontal
motion and automatic duplicate handling without providing these annotations to
the policy. It preserves the same detector, association and replay clocks.
