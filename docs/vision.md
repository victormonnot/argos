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

Open the printed local URL, then enable **Person detection** in the camera
header. **Flight controls** retains its usual mouse, touch and keyboard workflow.
The person scene keeps the declared 1.2-radian horizontal field of view fixed
by disabling the upstream automatic zoom plugin in a private gimbal copy.
The person's starting path is ahead of the camera; viewpoint, distance, motion
and model recognition determine whether a box appears. The default launcher
still opens the runway without a person or detector. `--scene person` and
`--vision-model` are independent choices.

For an already configured camera, add `--vision-model /absolute/path/to/model.onnx`
to `python -m argos.console`. The detector accepts the same JPEG camera data from
Gazebo or the existing V4L2 receiver. A physical camera/flight trial has not been
validated. Missing, altered or unsupported model files leave vision unavailable
with an explicit status; the application does not download replacements.

## Model and data flow

The model is the official Apache-2.0 [YOLOX-Tiny ONNX release](https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx),
with SHA-256 `427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7`.
[YOLOX documentation](https://github.com/Megvii-BaseDetection/YOLOX/tree/main/demo/ONNXRuntime)
describes its exported inference workflow; the model's
[upstream license](https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE)
is separate from ARGOS's MIT license. The scene's model attribution, pinned
checksum and animation limitations are in the [scene README](../examples/gazebo/README.md).

The ground computer runs OpenCV DNN on CPU with two inference threads. The displayed inference duration measures the
network call, excluding image decoding, preprocessing and postprocessing. Images
are resized with the model's 416 × 416 letterbox convention. Person confidence
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
