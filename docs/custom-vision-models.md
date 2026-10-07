# Custom YOLOX-Nano models

ARGOS can explicitly load a custom YOLOX-Nano ONNX bundle exported by IRIS or
another producer implementing the contract below. Official Tiny, Nano and S
models retain their pinned size/SHA-256 checks. Neither path downloads weights
at launch. A bundle hash proves which local bytes were used; it does not certify
the model's quality or identify its publisher.

## Bundle contract

Select a directory containing `manifest.json` and `model.onnx`, or the manifest
itself. The supported format is deliberately specific:

```json
{
  "format": "iris-yolox-onnx-v1",
  "architecture": "yolox_nano",
  "input": {
    "name": "images", "shape": [1, 3, 416, 416], "dtype": "float32",
    "color": "BGR", "range": [0, 255],
    "letterbox": {"alignment": "top_left", "value": 114,
                  "interpolation": "opencv_linear"}
  },
  "output": {
    "name": "output", "shape": [1, 3549, 6],
    "encoding": "yolox_raw_grid", "strides": [8, 16, 32]
  },
  "classes": [{"index": 0, "id": "person", "category_id": 1, "name": "Person"}],
  "model": {"path": "model.onnx", "size_bytes": 1234, "sha256": "<64 lowercase hex characters>"},
  "source": {"checkpoint_sha256": "<64 lowercase hex characters>",
             "model_id": "<source model ID>", "training_id": "<source training ID>"}
}
```

Replace the illustrative byte count and hashes with actual file evidence.
Classes have contiguous zero-based output indices, unique IDs, positive category
IDs and unique names. Exactly one name must be `person` (case insensitive).
Its output index can be any declared index; ARGOS never assumes custom class zero
is a person. With C classes, the output is `[1, 3549, 5+C]`: raw xywh grid values,
objectness and class probabilities, with inference grid decoding disabled at
export. ARGOS decodes the grid and multiplies objectness by the explicitly mapped
person probability. Confidence 0.35, NMS 0.45 and the existing tracker remain.

Input preprocessing is unchanged from official YOLOX: resize with OpenCV linear
interpolation, preserve BGR channels and 0–255 values, pad right/bottom with 114,
then use contiguous float32 NCHW. Export a self-contained ONNX graph compatible
with the installed OpenCV CPU runtime (the IRIS exporter uses opset 11).
External tensor files, custom Python code and alternate preprocessing contracts
are unsupported. The ONNX filename must be inside the bundle directory; the
loader rejects escaped paths, wrong size/hash, duplicate manifest keys and
incompatible declarations. On custom detector construction a padded input probes
the actual graph. Invalid output shape, nonfinite output and probabilities
outside 0–1 fail startup; runtime failure never falls back to official weights.

Additional provenance is permitted, for example dataset and training-recipe
identifiers. The manifest is limited to 1 MiB and the ONNX to 128 MiB. The same
verified bytes are passed to OpenCV; changing a file after its integrity check
does not change the loaded graph.

## Compare before selecting a model

Run on the ground computer, using the existing console/vision environment:

```sh
.venv/bin/python examples/compare_custom_vision.py \
  --frames-manifest /path/to/frames.json \
  --vision-bundle /path/to/custom-bundle \
  --threads 4 --output-dir /tmp/argos-custom-comparison
```

The pinned Nano baseline defaults to its model cache. Override with
`--baseline-model /path/to/yolox_nano.onnx`. For a native recording, replace
`--frames-manifest` with `--recordings-dir /path/to/recordings --recording <ID>`.
This uses the same native archive admission as the
[Tiny/S comparison](vision-comparison.md), which remains available unchanged.
No camera, radio, server, simulation or flight control is opened.

A frames manifest has `schema_version: 1` and a `frames` list. Every frame needs:

| Field | Meaning |
| --- | --- |
| `id`, `source_id` | Unique image ID and explicit image source |
| `path`, `sha256` | Local JPEG/PNG path (absolute or manifest-relative), SHA-256 of exact bytes |
| `width`, `height` | Image dimensions, checked against decoded input |
| `captured_at` | Finite nonnegative seconds on the original source timeline |
| `split`, `scene_group` | Preserved training/validation/test assignment and related-scene group |
| `clip_id` | Optional explicit continuous clip; a change resets tracking even for the same camera |
| `reference_status`, `boxes` | `human_validated` plus labeled `xyxy` pixel boxes, or unknown/null |

Example reference box: `{"label":"person","box":[12,34,56,100]}`. An empty
validated list is a negative reference; absent/null boxes remain unannotated.
Class names select person boxes only. Preserve shared capture/scene splits for
all nearby frames and derivatives. A split label alone does not establish data
independence or a fresh test set.

Frame order is preserved. Timestamps must increase within each explicit clip.
Source, clip, scene, split or dimension changes reset both trackers identically.
Without `clip_id`, each isolated photo resets tracking and contributes no
continuity observation. Original timestamps drive the tracker, regardless of
inference speed. Receipt timestamps remain receipt timestamps, not camera exposure
times. Images and manifest must retain their hashes throughout comparison.

`--split val` filters a manifest; otherwise results are grouped by split.
`--max-frames` bounds the run to 1–5,000 images (default 1,000), with exclusions
and truncation reported. Choose a new/empty output directory outside the source
directory. Output includes `report.json` and `frames.jsonl`; failed/interrupted
runs retain explicit state and `.partial` results. Neither source annotations
nor model files are changed.

The report gives TP/FP/FN, precision and recall at IoU 0.5 only where human boxes
exist. It also gives per-image detections, local track IDs, empty-detection runs
and CPU inference/processing times. **These continuity observations are not
target-loss or true identity-switch scores.** Sparse box references provide no
persistent identity truth. No winner or deployment decision is automatic.
Serial alternating execution with two warmups measures this host and this pass;
it does not reproduce live scheduling, target-laptop throughput or aircraft
behavior. The existing Tiny/S HTML exporter uses a different report format and
does not consume these custom-comparison reports.

## View the saved before/after images

Export an already completed frames-manifest comparison without executing either
model again:

```sh
.venv/bin/python examples/export_custom_vision_comparison.py \
  --comparison-dir /tmp/argos-custom-comparison \
  --output-dir /tmp/argos-custom-viewer
```

Open the resulting `index.html` directly. It embeds every original compared image,
saved boxes, confidence, local display IDs, split summaries and model/source
hashes. A divider compares baseline and candidate on the same image; sequence
navigation and autoplay retain each clip's original receipt intervals. Isolated
reference photos are explicitly a slideshow. Human boxes can be shown where
available, and missing annotations remain unknown.

The exporter verifies the completed report, each row's binding to the unchanged
source manifest, exact source image SHA-256 and dimensions. It runs no inference
and opens no device or service. The viewer contains no external scripts, fonts,
images or network calls. Copying `index.html` is sufficient for offline viewing;
`manifest.json` records export provenance and the HTML hash.

Use a new output directory outside source/comparison directories; existing paths
are never overwritten. Every frame is retained without resizing, re-encoding or
subsampling. The HTML is bounded to 64 MiB; a larger export fails explicitly.
This first custom viewer accepts the generic frames-manifest input; native
archive custom comparisons remain available as JSON results. The original
Tiny/S native-archive viewer is unchanged.

## Explicit selection and rollback

The console accepts `--vision-bundle /path/to/custom-bundle` instead of
`--vision-model`. The FLY and DST launchers accept the same option and saved JSON
field `vision_bundle`. Their default remains official Nano. The two model paths
are mutually exclusive; an explicit CLI selection overrides the saved selection
of the other kind. For a files-only launcher check:

```sh
.venv/bin/python -m argos.distance --config /path/to/existing-distance.json \
  --vision-bundle /path/to/custom-bundle --check
```

`--check` validates manifest and weights and reports their identity; it opens no
device. Actual graph compatibility is checked on detector startup and by the
offline comparison. Runtime manifests include model hash, manifest hash, class
mapping and source model/training identifiers. The console exposes that custom
identity in its vision status. Bundle replacement between service setup and
worker startup is rejected.

To return to official Nano, explicitly supply `--vision-model` with the verified
official path, or remove `vision_bundle` from the saved configuration. Keep each
model in its own directory; never overwrite official cache files. Selecting a
bundle does not tune tracking, confidence, control gains or expiry rules. An
offline improvement still needs timing and tracking validation on the intended
deployment host before a live model change.
