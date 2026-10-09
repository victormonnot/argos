# ARGOS FLY local workflow

[Documentation](README.md) · [Project overview](../README.md)

ARGOS FLY runs the physical-camera console and the continuous Pocket yaw stream
with one command. It is horizontal framing assistance: the pilot keeps throttle,
arming, roll and pitch. The software/profile are prepared for a combined
propeller-removed receiver check; their presence does not validate assisted flight.

## Prepare a known local installation

Use a reviewed Git revision containing this workflow. Preserve an existing dirty
checkout: after fetching that revision, an additional detached worktree can be
created with `git worktree add --detach ../argos-fly <reviewed-commit>`. Do not
reset an older laptop checkout just to remove its diagnostic patches.

In the chosen checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[console,vision,msp,radio-profile]'
```

Retain the verified official YOLOX-Nano file in
`~/.cache/argos/models/yolox_nano.onnx`, or supply its explicit path. The launcher
verifies its pinned hash; it does not silently download or replace weights. The
initial portable settings are Nano, four inference threads and ten analyses/s.

An explicitly selected [custom YOLOX-Nano bundle](custom-vision-models.md) is also
supported through `--vision-bundle` or the saved `vision_bundle` field. Compare
it offline first; official Nano remains the default and explicit rollback path.

Prepare the **single final ARGOS FLY model** using the
[profile workflow](edgetx-fly-profile.md). Keep its generated YAML on the laptop
as the launcher's `--profile` input, and install its matching `ArgFly.lua` and
model on the Pocket. The final profile preserves pilot channels and disables
crash flip separately from SC. Readback must confirm the native gate and Lua
script before receiver testing. The launcher validates the local artifact; it
cannot infer the SD card contents from the USB protocol greeting.

Identify the capture device using `v4l2-ctl --list-devices` and the Pocket using
`ls -l /dev/serial/by-id/`. Prefer the capture adapter's stable `/dev/v4l/by-id/`
link if present; the launcher resolves it to the current `/dev/videoN`. A camera
unplug/replug that renumbers the device may require relaunching. Use a stable USB
connection and exit Configurator or any other program owning either device.

## Save once, launch with one command

Example configuration preparation, replacing the camera/profile paths with the
verified local ones:

```sh
.venv/bin/python -m argos.fly \
  --camera-device /dev/video2 \
  --radio-port /dev/serial/by-id/usb-OpenTX_Radiomaster_Pocket_Serial_Port_00000000001B-if00 \
  --profile "$HOME/.local/share/argos/fly-profile/model.yml" \
  --save-config "$HOME/.config/argos/fly.json" --check
```

`--check` reads and validates files but opens no camera, serial port or network
listener. It prints the Git commit, dirty flag, actual runtime-byte hashes,
detector hash, profile hash, Lua hash and expected protocol. Saving refuses to
overwrite an existing configuration. Review an existing JSON to change paths.

Each subsequent launch:

```sh
.venv/bin/python -m argos.fly --config "$HOME/.config/argos/fly.json"
```

Open `http://127.0.0.1:8080`. Person detection is displayed automatically. The
Yaw assist panel distinguishes the visual demand from radio-reported activity.
Changing tabs, hiding the browser or closing its window does not cancel the
server-owned selection. Explicit Clear does cancel it and requires a new SC
cycle. Ctrl+C stops this launcher's console and stream. A second instance or an
occupied HTTP port is rejected before any camera or radio worker starts; the
launcher never kills an unrelated process.

Each run has a manifest, sampled status and event log under
`~/.local/state/argos/fly/runs/`; `latest.json` identifies the current run folder.
Logging runs separately from the USB sender with a bounded latest-state mailbox.
If the disk stalls, overwritten log samples are counted; this is not a lossless
serial recording. Cumulative counters remain in subsequent status samples.

## Select and recover without returning to the laptop

Use the on-screen switch symbols, not an ambiguous physical “top/bottom” label.
Observe **SC middle for about 0.2 s**, then **SC↑**, with yaw centered.

- If the selected person is still tracked, the cycle retains that person.
- Otherwise it selects the confident person nearest the image center. A near tie
  between two people is refused: reframe and cycle SC again.
- A failed selection does not wait indefinitely for somebody to enter the image.
  Another deliberate SC cycle is needed.
- Brief detection loss withdraws assistance and keeps a visual reference for up
  to **3 s**, while real images continue to arrive. The same track can recover;
  a new track needs one unique confident candidate, appearance similarity
  **at least 0.88**, and two distinct fresh images. Its center may move by at
  most **25% of image width** horizontally and **15% of image height** vertically,
  independent of person-box size. Width ratio stays within **0.5–2** and height
  ratio within **0.75–4/3**. These thresholds apply only to continuous recovery;
  ordinary tracking and the diagnostic preview retain their existing settings.
  Uniqueness alone is insufficient evidence of identity.
- Multiple strong candidates during recovery or an expired memory require a new
  SC selection. Missing/stale camera images and changed sources remove authority.
- Deliberate stick takeover stays manual until the SC cycle, even if vision has
  recovered. Initial thresholds are >25% yaw for 200 ms or >50% immediately via
  the native gate; these still need the Mode 2 ergonomics check.

Appearance similarity is a short visual association, not reliable human identity.
Similar clothing, occlusion and crossings can still fool it. The initial demo
should use a cooperative target and controlled framing. The operator remains
responsible for manual takeover.

Recovery diagnostics identify each evaluated image and candidate once, with
similarity, horizontal/vertical distance, size ratios, acceptance and its reason
(including waiting for the second image). Missing measurements are null; raw
appearance descriptors are never logged. Diagnostics are passed to the session
logger without performing disk I/O in the recovery decision. Each session run
writes these attempts to `recovery.jsonl` and logger status to
`recovery-status.json`. Ordinary attempts are not sampled; the asynchronous queue
is bounded and explicitly reports overflow and write errors rather than blocking
the control path.

## Demand delivery and the combined bench measurements

The integrated launcher publishes a validated latest demand directly from the
console owner, with no loopback HTTP or source polling thread. The serial thread
reads that immutable mailbox independently. New analyzed images can trigger an
earlier send, bounded to20Hz; a still-fresh command otherwise refreshes at10Hz.
Original image deadlines and radio ticket expiry are unchanged. Detector cadence
remains capped at10Hz. The standalone bridge retains its compact HTTP reader.

Status logs keep the `source_poll` metrics name for compatibility; `mode` is
`in_process` for the integrated source. Count, errors, wall time and thread CPU
describe publication/validation, **not total console/inference CPU**. Use image
receipt→SET/ACK measurements alongside live inference timing to assess latency;
neither is an exposure→aircraft-response measurement. A phone stopwatch shown
through the browser additionally includes browser display delay.

Protocol V3 reports observed radio A→T transitions and their cause, including
invalid target (`T`) versus expired ticket lease (`E`). Counts are based on
received status reports, so missed reports can miss transitions. During stable
tracking, record both these counters and received yaw continuity. The radio
lease remains **300 ms from ticket issuance**. Measure first; do not widen the
lease to hide a different input, gate or USB problem.

The grouped bench must use the final model, props removed and camera battery
power. Include deliberate takeover, brief occlusion/recovery, camera/USB loss,
reconnection, RF failsafe, and specifically:

1. SC↑ with tracking: move throttle as in Mode 2 flight without intentional yaw;
   assistance must stay enabled.
2. Compare a left-of-image demand with the known manual left-yaw direction
   received by Betaflight; repeat right. Correct mirroring/output direction
   before any assisted flight.

The subsequent short manual hover and low-authority assisted activation establish
actual sign, gain and response before filming. See the
[stream contract](edgetx-yaw-stream.md) for freshness and authority limits.
