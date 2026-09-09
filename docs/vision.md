# Camera-based person detection and tracking

ARGOS can detect people in the onboard image while the operator flies manually.
The optional **Person detection** switch displays bounding boxes, confidence and
short-lived track IDs. Each overlay accompanies the exact image analyzed by the
model. Turning the switch off restores the normal camera stream.

Detection alone supplies visual observations. The separately enabled
[visual framing controller](framing.md) can use an explicitly selected detection
for centering and apparent-size assistance in AltHold. Neither feature holds
position or estimates distance in metres. Track IDs associate nearby overlapping detections;
they do not identify a person or provide appearance-based re-identification.
After occlusion, fast motion or a scene/source change, an ID may change. Similar
people crossing may exchange IDs. Missing detections produce no predicted boxes.

## Prepare the optional model and scene

Use the existing [Gazebo/SITL installation](sitl-observation.md). From the ARGOS
repository, install the optional CPU inference runtime and explicitly download
the pinned model and ordinary animated person:

```sh
.venv/bin/python -m pip install -e '.[console,mavlink,vision]'
.venv/bin/python examples/setup_vision_model.py
.venv/bin/python examples/setup_vision_scene.py
.venv/bin/python examples/run_web_control.py \
  --ardupilot-dir ../ardupilot \
  --gazebo-dir ../ardupilot_gazebo \
  --scene person \
  --vision-model "$HOME/.cache/argos/models/yolox_tiny.onnx"
```

The setup helpers print their output paths. With `XDG_CACHE_HOME` configured,
use the model path printed by the helper instead of the path above. Setup is an
explicit network operation; launching the console and scene uses local verified
files. Model and mesh are not bundled in the Python package. Custom paths are
available through the setup helpers and launcher options.

**Tiny remains the default.** The optional `s` profile uses the larger official
YOLOX-S export on the ground computer. Prepare it explicitly and select the same
variant when launching:

```sh
.venv/bin/python examples/setup_vision_model.py --variant s
.venv/bin/python examples/run_web_control.py \
  --ardupilot-dir ../ardupilot \
  --gazebo-dir ../ardupilot_gazebo \
  --scene person \
  --vision-variant s \
  --vision-threads 4 \
  --vision-model "$HOME/.cache/argos/models/yolox_s.onnx"
```

Use the printed cache path if `XDG_CACHE_HOME` is configured, or setup's
`--output /absolute/path/to/yolox_s.onnx` with that same path at launch.
`--vision-variant tiny` selects the original profile explicitly; omitting the
option has the same effect. The path never selects a variant automatically:
each profile checks its own exact model size and SHA-256 before loading.
The example explicitly requests four inference threads. Both model profiles
still default to two when `--vision-threads` is omitted. The option accepts an
integer from 1 to 6 in either launcher or console; it changes the OpenCV worker's
CPU thread limit and does not reserve CPU cores. Four threads improved the
measured S latency on the evaluation host, but other host loads can differ.
The `s` profile improved detections on selected saved images but costs more CPU
time; see the [offline comparison and its limits](validation.md#optional-detector-profiles--september-9-2026).
That comparison alone does not validate S in an airborne framing loop; the
[live checks](validation.md#live-s-framing-checks) separately record the failed
two-thread run and the four-thread trial.

Open the printed local URL, then enable **Person detection** in the camera
header. **Flight controls** retains its usual mouse, touch and keyboard workflow.
The person scene keeps the declared 1.2-radian horizontal field of view fixed
by disabling the upstream automatic zoom plugin in a private gimbal copy.
The person's starting path is ahead of the camera; viewpoint, distance, motion
and model recognition determine whether a box appears. The default launcher
still opens the runway without a person or detector. `--scene person` and
`--vision-model` are independent choices.

For an already configured camera, add `--vision-model /absolute/path/to/model.onnx`
and, for S, `--vision-variant s` to `python -m argos.console`. The detector accepts
the same JPEG camera data from Gazebo or the existing V4L2 receiver. A physical camera/flight trial has not been
validated. Missing, altered or unsupported model files leave vision unavailable
with an explicit status; the application does not download replacements.

## Model and data flow

The catalog accepts two unmodified official ONNX exports from release `0.1.1rc0`:

| Variant | Official model | Input tensor | Output tensor | File size |
| --- | --- | --- | --- | ---: |
| `tiny` (default) | [YOLOX-Tiny](https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx) | float32 `1×3×416×416` | `1×3549×85` | 20,219,662 bytes |
| `s` | [YOLOX-S](https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx) | float32 `1×3×640×640` | `1×8400×85` | 35,858,002 bytes |

Pinned SHA-256 checksums:

- `tiny`: `427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7`
- `s`: `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063`

The S checksum was measured after downloading the official HTTPS release asset;
the release API did not supply a publisher digest. It pins the downloaded bytes,
not a publisher signature. Setup writes an attribution notice beside each model.
[YOLOX's ONNX guide](https://github.com/Megvii-BaseDetection/YOLOX/blob/6ddff4824372906469a7fae2dc3206c7aa4bbaee/demo/ONNXRuntime/README.md)
links these exports and documents their input sizes. The
[upstream Apache-2.0 license](https://github.com/Megvii-BaseDetection/YOLOX/blob/6ddff4824372906469a7fae2dc3206c7aa4bbaee/LICENSE)
is separate from ARGOS's MIT license. The scene's asset attribution, pinned
checksum and animation limitations are in the [scene README](../examples/gazebo/README.md).

The ground computer runs OpenCV DNN on CPU, defaulting to two inference threads.
An explicit `--vision-threads 1` through `--vision-threads 6` selects another limit.
The displayed inference duration measures the
network call, excluding image decoding, preprocessing and postprocessing. Images
use the selected model's fixed input size, preserving raw BGR values in 0–255,
with linear resizing and top-left letterboxing padded with 114, then float32
NCHW layout. This follows the
[upstream preprocessing](https://github.com/Megvii-BaseDetection/YOLOX/blob/6ddff4824372906469a7fae2dc3206c7aa4bbaee/yolox/data/data_augment.py).
Changing model size requires the matching export; the existing Tiny file is not
treated as a dynamic-resolution model. Person confidence
is objectness multiplied by the person-class probability, with a 0.35 threshold
and 0.45 nonmaximum-suppression threshold. At most 16 detections are retained.
Image-space association first uses one-to-one overlap matching, with a 0.7-second
memory limit. A conservative fallback associates mutually unambiguous boxes
across at most 0.35 seconds of lateral motion, bounded by center displacement
and body-box size changes. This reduces ID changes when a narrow person moves
farther than the overlap gate allows between analyses. Neither method predicts
boxes for missing detections. Association consumes image boxes and camera
reception times only.

A separate spawned process loads the model and performs at most five analyses
per second. Only one image can be awaiting inference, so intermediate camera
frames are skipped instead of building latency. The process receives JPEG bytes;
it has no MAVLink transport, vehicle pose, actor pose, depth or simulator labels.
A startup failure, crash or five-second inference timeout disables vision and
clears its results. Restart the console to retry; flight-control servicing has
its own existing lifecycle and continues independently of vision.

Vision status reports the configured model label, variant, input size and
`threads` limit.
Selecting S does not change frame-age limits, tracking gates or framing
thresholds. Its extra computation can reduce analysis frequency or make a result
too old for framing; a stronger detector is not an exemption from freshness.

The analyzed-image endpoint returns its original JPEG and compact normalized
boxes in `X-Vision-Result`, with the same run, camera, sequence and reception-time
headers as the raw-image endpoint. Results expire after one second, or sooner
if the camera's configured age limit is shorter. Changing camera/run clears
associations and rejects outstanding results from the previous source. A broken
camera invalidates the analyzed frame even if the last inference was recent.
The UI accounts for receipt age and transfer time; these are not exposure-time
or radio-latency measurements.

The model can miss people or detect unrelated shapes, particularly on synthetic
images, small subjects, unusual poses, occlusion or fast camera motion. This is
a perception baseline with visible failures, not evidence of reliable outdoor
following. No marker, downward optical flow, known body height or hidden target
coordinates corrects the model's output. Relative apparent size supplies the
optional framing objective; it is not a metric range measurement.

Journals continue to store received MAVLink only. Images, detections and track
IDs are live and are not replayed from Sessions. See [validation](validation.md)
for the actual checks and their limits.
