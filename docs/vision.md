# Camera-based person detection and tracking

ARGOS can detect people in the onboard image while the operator flies manually.
The optional **Person detection** switch displays bounding boxes, confidence and
short-lived track IDs. Each overlay accompanies the exact image analyzed by the
model. Turning the switch off restores the normal camera stream.

The [offline vision comparison](vision-comparison.md) reruns Tiny and S on the
same native recording images with the current tracker. It reports new detections
and processing costs without changing the live configuration.

Detection alone supplies visual observations. The separately enabled
[visual framing controller](framing.md) can use an explicitly selected detection
for centering and apparent-size assistance in simulated AltHold. For a physical
camera, the [horizontal yaw preview](#physical-camera-yaw-preview) displays a
proposed correction without sending commands. None of these features holds
position or estimates distance in metres. Short-lived track IDs combine box
geometry with compact appearance evidence from the analyzed image. They are not
persistent human identities or long-term re-identification. After occlusion,
fast motion or a scene/source change, an ID may change. Similar people crossing
or missing detections can still cause an identity swap. Missing detections
produce no predicted boxes.

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
the same JPEG camera data from Gazebo or the existing V4L2 receiver. Physical
assisted flight has not been validated. Missing, altered or unsupported model files leave vision unavailable
with an explicit status; the application does not download replacements.

## Physical-camera yaw preview

With a real V4L2 camera and a configured detector, **Observation** includes
**Yaw preview**. It calculates a horizontal correction from a selected person's
image position. It does not open a radio port, send MAVLink/MSP/RC commands,
acquire flight control, or require a telemetry connection.

Use the device showing the intended live camera (the device number can change
after reconnecting). Close other programs using that capture device, then launch
the console with the already prepared Tiny model:

```sh
.venv/bin/python -m argos.console \
  --camera-device /dev/video2 \
  --vision-model "$HOME/.cache/argos/models/yolox_tiny.onnx" \
  --vision-variant tiny --vision-threads 2 --port 8080
```

Open **http://127.0.0.1:8080**, stay in **Observation**, enable **Person detection**,
and select a person's box. **Yaw preview** shows the horizontal image error and
the proposed stick percentage from the latest analysis. Image and state requests
are independent, so this readout can briefly precede the displayed JPEG; both
must remain fresh and refer to the same source and selected target. Move the
person across the image to inspect left,
center and right behavior. **Clear** cancels the selection. No radio or flight
controller USB connection is needed for this preview.

The displayed JPEG keeps its usual bounded display lifetime (at most one second).
The proposed correction uses the newer server analysis and its stricter 450 ms
receipt-age limit. A delayed browser state response can temporarily show zero
while retaining the selection; it does not send a cancellation merely because
the displayed JPEG arrived later. A server-side stop clears the selection and
keeps its specific reason visible, even while later images are unavailable.

The error is `2 × (box_center_x − 0.5)`: zero at image center, negative on the
image's left, positive on its right. Within ±0.035 the proposed output is zero;
outside it the output is `0.25 × error`, capped at ±0.125 (±12.5% of a normalized
stick). These are initial preview parameters, not tuned flight gains. The sign
describes image coordinates; actual aircraft response, camera orientation and
radio mapping still require a separate disarmed integration test.

The selection uses server-owned detections paired with the displayed image.
Only a current detection with confidence at least 0.5 can produce a preview.
Missing detections, unavailable vision, a source or image-dimension change,
inconsistent image order, or a latest server analysis older than 450 ms clear the selected target
and zero the output. A returning person does not resume the preview until
selected again. The age is measured from local camera receipt, not inferred
sensor exposure time or radio latency; a capture device repeatedly delivering a
frozen upstream picture cannot be detected by this timestamp check alone.

Turning detection off or leaving Observation clears the browser's preview.
The browser also hides corrections when the displayed analyzed image or service
state expires. Repeated state reads do not refresh the camera receipt time.
This feature previews horizontal centering only; it does not maintain altitude,
distance or position, and it is separate from the fixed-pattern
[disarmed RF yaw bench](edgetx-rf-yaw-bench.md).

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
Image-space association retains its 0.7-second detection-memory limit.
Detections at or above 0.5 confidence establish tracks. Established tracks and
current strong detections compete first, so a new weak nested box cannot displace
the established target. New weaker measurements receive display IDs without
entering persistent association memory. A weak continuation may retain an
established ID only through mutually unique ordinary geometry. Neither a weak
measurement nor a strong measurement without a descriptor replaces or refreshes
the last valid strong appearance reference. Every measured detection remains
visible with its original box and confidence; this ordering does not suppress
a possible second person.

Ordinary overlap or nearby-box matching requires a mutually unique geometric
correspondence. If valid recent appearance descriptors disagree strongly,
similarity below 0.75 vetoes that edge before assignment. Ambiguous geometric
components are refused. An additional motion-bounded correspondence requires at
least 0.95 appearance similarity, a 0.08 mutual-best margin and at most 0.35 seconds
between images, together with center-displacement and body-size checks. The old
track must have appeared in the immediately preceding accepted analyzed image;
this additional match cannot bridge an empty observation.

A strong appearance reference older than 0.35 seconds is unavailable for both the
contradiction veto and the additional correspondence. Very small, clipped or
uninformative crops can legitimately supply no descriptor. In those cases only
mutually unique ordinary geometry remains available within its own gates; no
expanded match is permitted. An absent person, an old image or a changed source
does not justify selecting the nearest person.

The 208-value descriptor summarizes weighted color, brightness and gradient
histograms over four vertical crop bands. Background and central-crop weights
reduce some clutter; they do not segment the person. The score is appearance
evidence, not a calibrated probability of identity. Unique geometric matches
can still confuse people when other detections are missing, even with the
appearance veto. See the [recorded comparisons and remaining errors](validation.md#bounded-ambiguity-pause-and-appearance-association--september-9-2026).
No association path predicts boxes or uses vehicle pose, known body dimensions
or simulator identity. The current measured image remains the observation.

A separate spawned process loads the model and performs at most five analyses
per second. Only one image can be awaiting inference, so intermediate camera
frames are skipped instead of building latency. The process receives JPEG bytes;
it has no MAVLink transport, vehicle pose, actor pose, depth or simulator labels.
The same worker computes appearance descriptors statelessly from that exact
JPEG and its detections, using the existing OpenCV/NumPy runtime. Descriptors
are returned with the matching job result. The parent validates their fixed
size, finite bounded values and normalization, and stores them only when the
matching image is accepted. Source, sequence and receipt-time checks reject
outstanding stale results; worker history cannot become the parent’s accepted
image history. A change in image dimensions clears associations and selection
history, as a run or camera change does. Individual missing descriptors are
explicit entries in an aligned list; a malformed or missing list is an error,
not permission to fall back silently. Descriptors and raw crops are not added
to public API state.
A startup failure, worker/encoder error, malformed result or five-second inference
timeout disables vision and clears its results. Restart the console to retry; flight-control servicing has
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

Live camera status and its observation time are read under the same lock. A new
acquisition arriving during a reader call must not briefly appear to have a
future receipt and cancel a valid preview. Original image timestamps and the
450 ms yaw-preview limit remain unchanged. With detection enabled, actual
camera errors remain visible rather than being replaced by a generic detector
pending message. A device/driver failure still requires investigating the
capture connection and explicitly reopening the camera; it does not resume a
previously stopped target selection.

When selecting a person, the server consumes any completed analysis already
waiting in the worker queue before checking image expiry and selection revision.
A capability check must not expire an older frame first and unnecessarily reject
the click. A stop observed by an earlier request remains latched and cannot be
revived by a late selection response or a subsequently completed analysis.

The model can miss people or detect unrelated shapes, particularly on synthetic
images, small subjects, unusual poses, occlusion or fast camera motion. This is
a perception baseline with visible failures, not evidence of reliable outdoor
following. No marker, downward optical flow, known body height or hidden target
coordinates corrects the model's output. Relative apparent size supplies the
optional framing objective; it is not a metric range measurement.

Journals continue to store received MAVLink only. Images, detections and track
IDs are live and are not replayed from Sessions. See [validation](validation.md)
for the actual checks and their limits.
