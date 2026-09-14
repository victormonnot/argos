# Compare vision on recorded images

The offline comparison runs **YOLOX-Tiny and YOLOX-S on the same saved camera
images**, using the same current ARGOS appearance encoder and image tracker.
It produces a readable table and per-image results. A separate export makes
those results viewable on the original images in a local browser. No camera,
simulator, radio, web service or flight-control connection is needed.

This compares two detector variants. A later tracker change can be evaluated
again on the same capture, retaining each run's code hashes and results. The
current command always runs the current tracker with each detector; it does
not load a historical Git revision or change live console settings.

## Run a comparison

Use the existing console/vision Python environment and prepare **both** local
models with [the model setup instructions](vision.md#prepare-the-optional-model-and-scene).
The comparison verifies their pinned hashes and does not download models.
For a first run, the repository already includes a complete simulator recording:

```sh
.venv/bin/python examples/compare_vision.py \
  --recordings-dir examples/demo-flight \
  --recording 05aa147d371144c793c8f88ec3ef0509 \
  --threads 4 \
  --output-dir /tmp/argos-vision-comparison-example
```

For your own capture, use its recordings directory and the 32-character ID
shown in Sessions or its filename. Both the native `.jsonl` journal and matching
`.visual.sqlite3` sidecar are required. A video-only capture is supported: its
journal may contain zero MAVLink events. Arbitrary MP4 files are not inputs to
this first version.

`--tiny-model` and `--s-model` override the default local cache paths.
`--threads` accepts 1–6 and applies equally to both models; it defaults to 2.
`--max-frames` limits evaluated unique chronological images to 1–5,000, default
1,000. When that limit leaves archive rows unread, the report explicitly marks
the unevaluated tail. Omit `--output-dir` for a fresh temporary directory, or
choose a new/empty directory outside the source recordings directory.

The program exits with code 0 on completion, 1 on failure and 130 on interruption.
An interrupted or failed run retains a report and any partial per-frame output;
it must not be treated as a completed comparison. Source recordings are opened
read-only and their revisions must remain unchanged throughout processing.

## Read the result

| File | Contents |
| --- | --- |
| `report.md` | Tiny/S comparison table, observation examples and interpretation limits |
| `report.json` | Machine-readable summary, capture/model/code hashes, settings and execution environment |
| `frames.jsonl` | One row per evaluated image, original identity/times/JPEG hash, and each variant's new boxes, confidence, track IDs and timing |

The table counts images with and without detections, stronger detections and
display-ID transitions. Examples identify frame indices and playback positions
worth inspecting. The visual export below displays the newly computed boxes.
Opening the **original** capture in Sessions → Flight replay continues to show
its original recorded overlays; it does not load the comparison results.

`at_s` is the image's original receipt offset and may be negative when the first
image predates recording Start. `available_at_s` is when that archived image
became available; use it as the native replay cursor. Tracking uses the absolute
recorded receipt time, so processing speed cannot stretch or compress track age.

## View Tiny and S on the same image

Export an existing completed comparison; this command does **not** run either
model again:

```sh
.venv/bin/python examples/export_vision_comparison.py \
  --comparison-dir /tmp/argos-vision-comparison-example \
  --output-dir /tmp/argos-vision-view-example
```

The original native capture must still be available. The exporter checks the
comparison's recorded source/model metadata and each selected image against its
archive identity and JPEG hash. If you moved the recording files, add
`--recordings-dir /absolute/path/to/recordings`; the journal and visual sidecar
must still match the comparison. This locates the existing inputs, rather than
creating a new evaluation.

Choose a new/empty output directory outside the input directories, or omit
`--output-dir` for a fresh temporary folder. Existing reports and recordings
remain unchanged. The export contains:

| File or directory | Contents |
| --- | --- |
| `index.html` | Local comparison viewer and its recorded Tiny/S results |
| `images/` | Original JPEGs used by the comparison |
| `manifest.json` | Export provenance, source hashes and image bindings |

Open `index.html` directly in a browser. Copy the **whole folder**, including
`images/`, to view it on another computer; viewing needs no Python environment,
model files, console or running service.

The two views use the same image and stay on the same frame when navigating.
Each shows its model's recomputed boxes, confidence and display IDs. You can
jump to images where Tiny and S return different detection counts. This shortcut
does not find every difference in box placement or confidence, and it does not
match identities across the two models. Reviewing the images helps interpret
the counts; the viewer supplies no ground-truth labels or accuracy score.

This is a viewer for the saved comparison, with the same selected images,
exclusions and sampling limits. It does not change live settings or establish
physical tracking or flight readiness.

## What the measurements mean

- **Same inputs and tracking.** Both models receive identical JPEG bytes. Every
  detector output passes through the existing appearance encoder and tracker;
  detector thresholds, tracker settings and flight guidance are unchanged.
  Each variant owns independent tracking state, reset on source/dimension changes.
- **Observed detections, without truth labels.** An image without a detection
  does not establish a missed person. Extra boxes can be false detections, and
  fewer display IDs do not prove better identity continuity. Numeric IDs are
  local to each run. The report does not assign a winner or calculate accuracy.
- **Recorded subset.** The archive contains sampled, JPEG-encoded images, which
  may have been selected by the original detector's live schedule. Exact image
  duplicates are counted once. Older completed results with non-increasing
  receipt times are explicitly excluded, preserving archive availability order.
  Gaps and excluded inputs are unknown; no continuous absence is inferred between
  samples. This does not recreate either model's live frame selection.
- **Offline timing.** Network inference and complete processing are reported
  separately. Processing includes JPEG decoding, detection, appearance encoding
  and tracking; it excludes archive I/O, model loading and writing the report.
  Two warmups per model are excluded from timing and tracker state. Models run
  serially, alternating which runs first per image. These one-pass measurements
  describe this host, not live FPS or camera-to-command latency.

Simulator captures verify the tool and help inspect differences. A real FPV
capture is needed to assess the images from that camera, and a physical trial
is still needed to evaluate the complete acquisition/control path.
