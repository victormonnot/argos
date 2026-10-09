# Offline selected-person diagnosis

[Documentation](README.md) · [Project overview](../README.md)

`examples/diagnose_continuity.py` diagnoses the current detector, local image
association, continuous target selection and dry yaw admission on completed
native `.flight` recordings. It opens no camera, console server, radio or flight
transport and changes no runtime settings. This is a diagnostic baseline, not
a comparison of alternative trackers or a closed-loop flight evaluation.

The separate [tracker comparison](tracker-comparison.md) reuses this replay
boundary with one frozen custom-detector cache and offline ByteTrack/BoT-SORT
adapters. It leaves the production tracker selection unchanged.

Run in the existing ARGOS vision environment with locally provisioned official
YOLOX-Nano weights:

```sh
.venv/bin/python examples/diagnose_continuity.py \
  --flight-dir /path/to/recording.flight \
  --windows /path/to/windows.json \
  --threads 4 --max-hz 10 \
  --output-dir /path/to/new-diagnostic
```

The archive must contain finalized `manifest.json`, `camera.json`,
`camera.frames.jsonl`, `camera.mjpeg` and `events.jsonl`. Inputs and weights are
read only. Output must be new or empty and outside the archive. The current
reader requires one physical-camera source and constant dimensions. Declared
recorder losses remain limitations; a completed writer does not imply zero
losses. Inconsistent source bindings, index structure or timelines are errors.

Supply explicit windows on the original capture's elapsed receipt clock:

```json
{
  "windows": [{
    "name": "loss-example",
    "start": 10,
    "end": 25,
    "selection": {
      "at": 12.1,
      "box": [0.4, 0.2, 0.15, 0.6],
      "min_iou": 0.5
    }
  }]
}
```

Boxes use normalized `x, y, width, height`. At the selection instant, exactly
one current strong detection must overlap the supplied box above `min_iou`.
Selection is attempted once; failure is reported and never silently retried.
Original numeric track IDs are not restored. Each window starts with a fresh
tracker: include enough images before the controlled selection, and do not call
this a reconstruction of historical internal state or operator intent. Windows
are bounded to 120 seconds each, eight windows and 5,000 images maximum; the
default image limit is 1,000. Inputs exceeding the limit fail without truncation.

## Two distinct replay schedules

- **Recorded observation schedule:** reruns inference on original JPEGs, feeds
  association only for source sequences present in saved vision events, and
  delivers results at their logged observation times. Those times are upper-bound
  proxies for availability, not exact worker completion times. Intermediate
  preview/consumer reads use a declared 10 ms grid. Missing events do not prove
  an image was never analyzed. Historical discarded worker results are unknown.
- **Desktop scheduling model:** exercises production `VisionService` with a
  fake worker, original camera receipts and cached measured decode/detection/
  appearance durations. It preserves one pending job, latest-image selection,
  maximum inference rate and result admission. Worker completions are collected
  at service ticks. IPC, capture encoding, other owner work and hardware delivery
  are not measured or modeled. `--extra-worker-ms` adds an explicitly synthetic
  delay in this mode only; it is useful for deadline diagnostics.

Both modes run production `ImageTracker`, `YawPreview(continuous=True)` and
`YawValidator`. Results are delivered before selection/expiry decisions at the
same replay instant. State reads between images can latch a stale-image stop.
Re-reading one image cannot count as a second recovery image. Dry consumer
validation uses a copy and retains state only after success, as the in-process
source does. No pilot authority, radio lease, Lua output or aircraft response is
inferred from a valid dry demand.

## Evidence and interpretation

The output directory contains:

- `report.json`: source/model/code hashes, limits, numerical agreement with
  historic detector outputs, software intervals and events for both modes.
- `inference.jsonl`: detections, appearance descriptors and measured processing
  cost for each original image, with source sequence, receipt and JPEG hash.
- `index.html` and `frames/`: offline evidence viewer with byte-exact JPEGs,
  historical/recomputed boxes, frame navigation and replay event inspection.

Association diagnostics expose geometry and appearance rejection reasons,
ambiguities, expirations and weak display-only IDs. They are opt-in via
`ImageTracker(on_diagnostic=...)` and do not change associations. Existing target
recovery diagnostics add confidence, crop availability, appearance similarity,
geometry, second-image confirmation and expiry reasons. Predicted track identity
must never be treated as a human label.

Software phase durations and dry-valid intervals do not measure time following
the correct human. Evaluating that requires reviewed temporal identity, intended
target/selection, boxes, visibility/occlusion/off-screen states and potential
distractors. Sparse annotations or target-only labels cannot support global MOT
metrics. Keep training scenes, reused validation scenes and independent tests
separate. An explicit unknown is preferable to an invented cause.

Timing reports include stage wall/CPU distributions and cached replay execution
cost. Two warmups and one image pass give diagnostic desktop measurements only.
The reported RSS is the harness process high-water mark at the end of replay,
including loaded weights, cached JPEGs and traces; it is not live worker memory
or a comparison across trackers. Synthetic worker durations and image age on the
replay clock must remain separate from actual elapsed harness execution. Camera
receipt is not exposure time or end-to-end aircraft response latency.

Source hashes are checked before and after diagnosis. Failed/interrupted runs
retain an explicit report state and partial inference output. No live model
selection or deployment follows automatically.
