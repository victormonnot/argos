# Offline motion-assisted target recovery

[Documentation](README.md) · [Project overview](../README.md)

`examples/compare_recovery.py` tests whether short image-space motion history
helps selected-person recovery after ByteTrack changes an identifier. It reuses
the [tracker comparison](tracker-comparison.md) cache and the separate
[reviewed reference](continuity-validation.md). It does not change production
selection, association, the detector, an active configuration or flight behavior.

```sh
dist/tracking-venv/bin/python examples/compare_recovery.py \
  --source-comparison /path/to/completed-comparison \
  --reference /path/to/reference.json \
  --candidate-review /path/to/detection-review.json \
  --protocol /path/to/protocol.json \
  --previous-comparison /path/to/completed-version-1-recovery-comparison \
  --output-dir /path/to/new-recovery-comparison
```

Use the preceding comparison's isolated environment. The output directory must
be new or empty. The protocol fixes policy constants before inspecting new
results. Existing reference and candidate-review files are evaluation inputs,
not inputs to the policy. They must not be silently relabeled to suit an outcome.
Protocol version 2 requires `--previous-comparison` to verify the unchanged
legacy policies against the completed version 1 experiment. Protocol version 1
retains its three original policies and can still run without this argument.
Version 3 adds candidate qualification and binds a completed version 2 report;
all five previous policies must reproduce their complete semantic results.

## Compared policies

All conditions use the same ByteTrack association, native identifiers, original
detector boxes, cached heuristic appearances, source images, initial selection
and replay clocks. No learned ReID network is added. The runner compares:

| Policy | Recovery input and horizontal geometry |
| --- | --- |
| `current` | Existing recovery behavior |
| `motion` | Existing observations, with horizontal motion recalculated from recent history |
| `motion_duplicates` | Recalculated motion and strict duplicate grouping before recovery |
| `motion_held` | A bounded motion estimate retained from an actual selected measurement (version 2) |
| `motion_held_duplicates` | Retained motion and the same strict duplicate grouping (version 2) |
| `motion_held_candidates` | Retained motion and explicit qualification during a lost/paused multi-candidate episode (version 3) |

Version 1 compares the first three policies. Version 2 compares the first five;
`current`, `motion` and `motion_duplicates` are legacy parity controls. The
viewer derives its selector and tables from the report's actual policy list,
including when displaying a version 1 report.
Version 3 compares all six. Construct experiments through `make_preview`, which
selects the appropriate implementation.

In `motion` and `motion_duplicates`, history contains only recent, measured, strong observations of the
selected target. Every retained sample must be at most 0.7 seconds old relative to the candidate
image receipt; at least two distinct eligible receipts are required. If an older
sample expires, recovery falls back to static geometry even when the latest
anchor is less than 0.5 seconds old. Horizontal velocity comes from the last two
eligible measures,
limited to one image width per second. Prediction advances at most 0.5 seconds;
the candidate must lie within the existing 0.25 image-width horizontal tolerance
of that prediction. Insufficient motion evidence retains the existing behavior.
Motion remains an association aid: it never manufactures a detector observation
or a fresh measurement for a skipped image.

The `motion_held` policies calculate and retain a scalar horizontal velocity
when an actual strong selected measurement arrives. Its two measurements must
have distinct, increasing sequence numbers and receipts no more than 0.7 seconds
apart. The latest measurement becomes the estimate's anchor. Velocity is capped
at one image width per second, as in the recalculated policy. The estimate remains
eligible only until 0.5 seconds after that anchor: pruning the older history sample
does not erase it early. A rejected candidate, repeated owner read or pending
recovery does not renew the estimate or its anchor. Once expired, it falls back
to the existing static geometry. Selection changes clear this history.

For these retained estimates, horizontal gating uses
`effective_dx = abs(candidate_cx - predicted_cx) + 0.1 * horizon_s`, with the
existing limit of 0.25 image widths. The additional term is an explicitly fixed,
**uncalibrated assumption** of 0.1 image widths per second. It tightens the gate
as the estimate ages; it is neither a statistical confidence bound nor a motion
or identity guarantee. Diagnostic events retain `motion_residual_x`,
`motion_uncertainty_margin_x` and `motion_effective_dx`, together with the
source measurement and anchor identifiers and times. Rejected candidates never
become selected measurements through this prediction.

Appearance similarity still requires 0.88. The existing vertical tolerance of
0.15, scale checks, confirmation over two images and recovery deadline remain.
A terminal stop remains terminal. Passing geometry alone is insufficient to
recover an identifier. Strong detection confidence remains 0.5.

The duplicate variant groups only strong candidates whose **every pair** meets
both IoU 0.85 and appearance similarity 0.95. This pairwise requirement prevents
a chain of partial overlaps from merging unrelated endpoints. Missing appearance
or a failed pairwise check leaves candidates separate. Within a valid group,
retain the current selected identifier first, then a pending recovery identifier,
then the highest-confidence candidate, with source order breaking ties. Raw
association results are retained; only the observation passed into selection is
filtered. This policy does not consume reviewed person identities or manually
identified duplicate locations.

These thresholds define this bounded experiment, not a general duplicate or
identity guarantee. Nearby distinct people can look similar. Tests with competing
people, unavailable evidence and delayed observations establish limited software
contracts; they do not establish a real-world wrong-person rate.

## Candidate qualification

The version 3 candidate variant retains every raw box. It starts a competition
episode only when the selected target is lost or paused and more than one strong
detection is present. Outside that episode, the held-motion behavior remains
unchanged, including trust in a returning solo native ID. An intact selected ID
can continue beside another person, as it already does in production.

During an episode, every candidate, including the old selected ID, must pass
appearance, geometry and receipt gates. Recovery requires exactly one eligible
candidate. A geometrically plausible competitor with unknown appearance blocks
recovery; a known competitor less than 0.05 below the winner also blocks it,
even if that competitor is just below the 0.88 eligibility threshold. Multiple
eligible candidates remain ambiguous regardless of score separation. The margin
is a fixed assumption, with a 1e-12 numeric tolerance, not calibrated identity
confidence. Weak detections do not vote; geometrically incompatible candidates
remain visible but do not block recovery.

Entering an episode or encountering any refusal clears pending confirmation.
The episode persists when a competitor disappears: two newly qualified images
with increasing sequences and receipts are still required. Ambiguity holds zero
correction until the existing deadline; it cannot renew the last selected
measurement, appearance reference or motion estimate. Confirmed recovery ends
the episode, while the unchanged dry consumer still applies its own admission
checks. Stops and authority resets clear the episode.

Two identical-looking people can pass the same gates, and a different person
inheriting an active native ID can remain selected. Compatible duplicate boxes
also remain ambiguous in this variant. The
[synthetic multi-person qualification](multi-person-qualification.md) exposes
these limits with evaluator-only symbolic truth. It does not supply real video
identity validation or change the production multi-candidate stop.

## Reproducibility and resource scope

The runner retains each complete source window, including history before the
loss. Both recorded availability and simulated scheduling are compared. Each
policy/clock combination runs three times in fresh subprocesses, each processing
both windows. Semantic repeatability is checked independently of execution costs.
A changed result must be attributable to selection/recovery: detector outputs,
raw associations and the delivery schedule must remain unchanged.
Version 2 additionally binds the previous completed recovery report and requires
semantic parity for all three legacy policies, independently of execution costs.
Version 3 requires parity for all five preceding policies and binds the new
candidate policy source as part of its implementation provenance.

CPU, wall time and process memory concern this cached replay on the desktop.
They do not include fresh detector inference or establish laptop or physical
flight latency. Execution costs are not injected into the shared replay clock.
The recorded clock uses logged availability proxies; the simulated clock omits
owner blocking and interprocess communication. Report scheduling assumptions
beside results instead of presenting either clock as a complete live replica.

## Reading the result

`report.json` stores complete frame decisions, events, provenance and comparisons.
The standalone `index.html` synchronizes baseline and prototype on the exact
original frame. It marks retained and removed boxes separately, retains native
track IDs, and highlights a selected box only when it is a fresh strong measure
on that image. A nonadmitted source image never inherits a preceding tracker box.
Approximate reference contours appear only when an annotation exists.

The first table reports software state and dry-admission durations over the
**full replay window**. The second table reports reviewed selected-target counts
only inside the **dense reference span**. Its denominator is the number of
admitted images with sufficiently clear visible references, not every source
image. Other-person, nonperson and unknown selections remain separate. An
uncertain reference is not treated as a missing person or a correct selection.

A longer software tracking duration is not sufficient evidence of better person
continuity. Check which candidate was selected, whether recovery confirmed on
two images, whether a stop merely became an unresolved pause, and whether the
same rule harms a previously successful cadence. Annotation quality and identity
coverage limit these conclusions. Assistant review remains explicitly separate
from human validation; reused passages are not independent validation scenes.

This experiment does not authorize deployment. Independent recordings, broader
identity review, laptop timing and any production integration are separate work.
