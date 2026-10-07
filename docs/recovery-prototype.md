# Offline motion-assisted target recovery

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
  --output-dir /path/to/new-recovery-comparison
```

Use the preceding comparison's isolated environment. The output directory must
be new or empty. The protocol fixes policy constants before inspecting new
results. Existing reference and candidate-review files are evaluation inputs,
not inputs to the policy. They must not be silently relabeled to suit an outcome.

## Compared policies

All conditions use the same ByteTrack association, native identifiers, original
detector boxes, cached heuristic appearances, source images, initial selection
and replay clocks. No learned ReID network is added. The runner compares:

| Policy | Recovery input and horizontal geometry |
| --- | --- |
| `current` | Existing recovery behavior |
| `motion` | Existing observations, with bounded horizontal motion prediction |
| `motion_duplicates` | Motion prediction and strict duplicate grouping before recovery |

Motion history contains only recent, measured, strong observations of the
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

## Reproducibility and resource scope

The runner retains each complete source window, including history before the
loss. Both recorded availability and simulated scheduling are compared. Each
policy/clock combination runs three times in fresh subprocesses, each processing
both windows. Semantic repeatability is checked independently of execution costs.
A changed result must be attributable to selection/recovery: detector outputs,
raw associations and the delivery schedule must remain unchanged.

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
