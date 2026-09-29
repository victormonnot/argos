# Real-camera vision to an unused EdgeTX channel

This bench connects the console's **Yaw preview** to **CH32** on a Pocket with
both RF modules OFF. A selected person's horizontal position in the real camera
image drives a small correction on the radio's channel monitor. The helper reads
the console's existing analysis; it does not start a second detector or send
MAVLink, MSP or flight commands.

Use a Pocket running EdgeTX 2.12.4 with the already verified
[native guard configuration](edgetx-native-guard-bench.md). This exercise checks
the new vision-to-radio connection. It does not repeat the fixed-pattern and
held-heartbeat tests, and it does not enable aircraft control.

## Prepare the camera and a separate radio model

Remove the aircraft's propellers and keep it disarmed. Its camera may need the
aircraft battery to produce video; USB power alone is not required for this
exercise. Keep both radio modules OFF throughout. No flight-controller USB
connection or Betaflight readout is used. This is a separate setup from the
[RF yaw bench](edgetx-rf-yaw-bench.md), which requires an aircraft powered only
by USB and must not be run with a battery.

With both RF modules OFF, the Pocket will **not connect to the aircraft's
receiver**. This is expected for this bench and is not evidence of lost binding.
The separate video-receiver connection can still supply the camera image.
Keep the RF settings OFF; do not rebind or enable a transmitter to fix a USB
handshake or an image-timing error.

1. Duplicate the verified **ARGOS USB** model, including its CH32 mixes and
   logical switches, into a new model named exactly **ARGOS VIS**.
   Use this short name, which fits the Pocket's model-name field.
   Select this copy. Keep **Internal RF = OFF** and **External RF = OFF**.
   Do not copy ARGOS RF: that profile uses a flight channel instead of CH32.
2. With the powered radio connected in USB Storage mode, copy
   `scripts/edgetx/ArgVis.lua` to the SD card's **SCRIPTS/MIXES/ArgVis.lua**.
   Safely eject and unplug.
3. In **CUSTOM SCRIPTS / LUA1**, select **ArgVis** in this copy. Its outputs are
   **Val, Fsh, Seq, Hbt**, in the same order as ArgMix. The existing CH32 sources
   and L01–L07 references must continue to use this LUA1 slot.
4. Preserve both CH32 rows: the first is the unconditional manual **Rud** source;
   the second is **LUA1 Val**, **Replace**, gated by **L07**. Preserve the native
   logical switches and the actual PC selector already verified. Leave the
   selector in manual while setting up. CH32's output settings must remain
   neutral/default, with no curve, reverse or offset.
5. Keep **USB-VCP = LUA**. Connect the Pocket's USB data port to the computer
   running ARGOS and select **USB Serial**. Return to **CHANNELS MONITOR**,
   channels **25–32**, to watch CH32. Do not open ArgosUSB or another Lua Tools
   script alongside the model's mixer script.

The original ARGOS USB and ARGOS RF profiles and scripts remain available for
their respective diagnostics. ArgVis checks its model name and both module
types on each callback. These configuration checks are not an electrical
measurement of RF output or a substitute for the operator's OFF settings.

## Start the console and select a person

Use the existing ARGOS installation and prepared person-detection model. The
helper additionally needs `pyserial==3.5`, provided by the `msp` extra:

```sh
.venv/bin/python -m pip install -e '.[msp]'
```

Find the capture adapter's current image device with `v4l2-ctl --list-devices`;
its number may change after reconnecting it. Close other applications using
that camera. Stop an older ARGOS server before relaunching on the same port.
For example, replacing `/dev/video0` with the actual capture device:

```sh
.venv/bin/python -m argos.console \
  --camera-device /dev/video0 \
  --vision-model "$HOME/.cache/argos/models/yolox_tiny.onnx" \
  --vision-variant tiny --vision-threads 2 --port 8080
```

Open **http://127.0.0.1:8080 → Observation**, enable **Person detection**, and
click a person's box. Check that **Yaw preview** is active and follows their
horizontal position. Keep that person selected before starting the helper;
an idle or stopped preview cannot authorize a session.

The helper is separate from the web interface. Merely starting the console or
selecting a person does not open a radio port. The helper reads only a local
console on `127.0.0.1`; the console, camera and Pocket must therefore be
connected to the same computer for this guide.

## Run one finite vision session

Stop any old serial probe. Identify the Pocket with
`ls -l /dev/serial/by-id/` and use its actual stable path, not a flight-controller
port. With the yaw stick centered, select the previously verified PC switch
position. In a second terminal, from the ARGOS checkout:

```sh
.venv/bin/python -m argos.backends.edgetx_vision_bench \
  --port /dev/serial/by-id/YOUR_POCKET_DEVICE \
  --console-port 8080 --duration 20
```

The default duration is 20 seconds; the accepted range is 1–30 seconds. The
helper establishes a new ArgVis session and sends bounded values at up to
10 Hz, using the console's existing detections. There is no automatic retry,
reconnection or session restart.

The browser preview can remember a briefly missing person with zero correction.
This does not resume the bench: entering that pause changes the preview revision,
so an existing radio session stops even if its HTTP reads miss the pause itself.
A new bench invocation is required after recovery.

After receiving the expected greeting, the host starts its single BEGIN write
with an invalid marker line (`#` followed by a newline). This discards any
unfinished input left in the radio's line parser before the new BEGIN. Appending
the marker makes an old partial command invalid; merely adding a newline could
finish that command. Clearing the PC's receive buffer does not clear the radio's
input buffer. This synchronization sends no SET and does not retry BEGIN or
resume an expired session. It cannot repair competing serial readers or missing
USB delivery.

After the handshake, the helper waits up to one second for an analysis with
enough remaining time for streaming. It sends no channel value while waiting.
Each streaming value requires at least 140 ms of radio lifetime: the 100 ms
send interval, 20 ms write budget, 10 ms radio tick and one 10 ms polling period.
With the new value's write/tick reserves, this requires at least 170 ms
remaining on the original image deadline at the final send check. An older
but still valid preview is polled again after 10 ms
instead of sending a value that must expire before the next legal send slot.
During active output, that wait ends 30 ms before the preceding command's
modeled expiry, preserving its write/tick reserves, and is also bounded by
the existing 250 ms gap limit and the finite session. Errors, source/selection
changes or expiry remain terminal. Waiting cannot renew an image timestamp.
The old value's transfer reserve is checked again immediately before writing
the next value. These budgets do not guarantee HTTP, OS or physical USB timing;
a slow request can still stop the session. This correction can stop an attempt
earlier and is not a hardware continuity fix: a stable browser preview alone
does not establish enough margin for uninterrupted radio output.

For recurring timing stops with stable video and a successful handshake, use
the [offline thread comparison](vision-performance.md) to assess CPU settings
before another live attempt. Its results identify candidates, not an automatic
fix or proof of a sustainable camera-to-radio cadence.
When live measurements show the worker is fast enough but fresh results remain
limited by the default five-Hz ceiling, the console's `--vision-hz 8` is a
candidate. Validate that rate with passive live timing first. It increases only
the analysis ceiling, leaving at most one pending image; the bridge still sends
at most ten values per second and keeps its original expiry rules. A higher
ceiling cannot eliminate camera, CPU, HTTP or USB delays.

For a measured, steady analysis cadence near ten results per second, an optional
policy sends each analyzed image at most once:

```sh
.venv/bin/python -m argos.backends.edgetx_vision_bench \
  --port /dev/serial/by-id/YOUR_POCKET_DEVICE \
  --console-port 8080 --duration 20 --new-frames-only
```

This policy avoids refreshing the output from an aging image just before a new
analysis arrives. It waits for an initial image that permits the full existing
200 ms command lifetime, then requires a newer analyzed frame for every command.
Later commands retain the 140 ms minimum lifetime. Reads begin 10 ms before the
next permitted send time, with 5 ms polling while waiting for a suitable result;
a read completed before that time is checked again through a new HTTP request.
Commands remain limited to ten per second, and all image, session and previous
command deadlines are checked before writing. A missing result still stops the
session, with no automatic restart.

The option leaves the default policy unchanged. It is unsuitable when results
regularly arrive too far apart: even a 200 ms command provides only 170 ms before
the existing transfer reserve. It also increases HTTP reads, so software replay
success must be followed by a live RF-off check on the actual computer. No radio
script or model change is needed to try this host-side option.

Keep the ARGOS browser page visible beside the helper terminal. Minimizing the
browser or changing its tab clears the preview selection; merely focusing a
separate terminal does not. Updating or extracting this standalone helper does
not update the running console. Apply the corresponding console changes, restart
its server and reload its page before retesting a console fix.

If the greeting times out, check the exact short model name **ARGOS VIS**,
ArgVis in LUA1, both RF modules OFF, and USB-VCP/USB Serial settings. An older
ArgVis script requiring `ARGOS VISION` must be replaced on the SD card; renaming
the model alone will not change that script's guard. Camera, detector and preview
failures include their reported state/detail in the helper's error message.
Stabilize the live preview before retrying; racing from a click to the terminal
does not repair a disappearing camera.
The periodic `ARGOS_USB_VISION_BENCH_V1` greeting can appear several times in one
USB read. It is normal, including before the handshake. Deadline failures report
the limiting image/session time and remaining command budget; they do not imply
that another radio handshake or a faster click can make the analysis newer.

During the run:

- Move left, center and right **in the image**. CH32 should follow the selected
  person's correction, with a maximum of **±12.5%**. The center deadband gives
  zero. Positive/negative values describe image error, not a verified physical
  aircraft rotation direction.
- Move the selector to manual, or move yaw clearly outside its central 10%:
  CH32 should follow the stick while the helper can continue acknowledging
  messages. This reuses the existing native gate. Recentering with PC still
  selected permits fresh PC values again; choose manual to retain control.
- Press **Clear** in Yaw preview, or leave the camera image. Once the preview
  stops, the helper must stop too, and CH32 must return to the manual mix.
  With the stick centered this is near zero; with an off-center stick it must
  follow the stick instead of being forced to zero.

After a stop, select a person again when needed and run the command explicitly
for another session. Selecting another person or clearing and reselecting during
one run ends that run; it does not silently retarget an active connection.

If checking this bridge's physical USB-loss behavior, unplug only the Pocket's
USB cable during a nonzero correction, keeping the radio powered. CH32 should
leave the PC value and follow the manual stick; the host's disconnect error is
expected. No repeat of the earlier ArgHld polarity tests is required here.

At the duration limit, or on **Ctrl+C**, the helper stops sending and closes its
port. It sends no final cleanup command: radio-side expiry and the native gate
provide fallback when PC output ends. Finish with the selector in manual, leave
both RF modules OFF, and disconnect the aircraft battery used for video.

## Freshness and limits

The host binds one run to the preview's run ID, video source, selection revision
and target. It checks the server-owned correction and the original image receipt
time. Missing/changed selection, stale or inconsistent data, changed source,
console errors and serial errors stop the run. Repeated reads of the same frame
do not make that frame newer.

The preview's analysis age limit is **450 ms** from local image receipt. Each
radio value carries a shorter lifetime, at most **200 ms**, encoded in 10 ms
ticks. Values from the same analysis use the same image deadline; their allowed
lifetime shrinks as that deadline approaches. This does not measure the camera sensor's
physical sample age, complete video latency or the time bytes arrive at the
radio.

ArgVis uses its own `ARGOS_USB_VISION_BENCH_V1` protocol, separate from the earlier
fixed-pattern scripts. It accepts continuous raw values from −128 to +128,
fresh sequence numbers, and lifetimes from 1 to 20 ticks. Its session has a
30-second limit and accepts at most 300 value messages. Expiry latches the
session inactive until a new handshake; later data cannot restart an expired
session. These script checks rely on Lua callbacks continuing to execute.

The native heartbeat gate remains necessary when Lua stops updating. A host
ACK means the script accepted a message; it does not prove CH32 configuration,
RF delivery, motor response, physical framing or a flight failsafe. The existing
automatic return to PC after recentering is a bench policy, not a selected
flight-control policy. No receiver telemetry is added to ARGOS by this helper.

Software checks use synthetic HTTP/serial exchanges, clocks and Lua API mocks:

```sh
.venv/bin/python -m pytest -q \
  tests/test_edgetx_vision_bench.py tests/test_vision_bench_source.py \
  tests/test_vision_bench_cadence.py
lua tests/edgetx_usb_vision_test.lua
```

They do not run a real camera, the radio's native mixer or a physical RF link.
