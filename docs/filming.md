# Local filming captures

[Documentation](README.md) · [Project overview](../README.md)

The FLY and DST consoles can record one take as a camera archive plus timestamped
pilot-stick, radio-status and vision observations. Start and Stop control the
recording only. They do not arm the aircraft, select a target or change assistance.

Camera recording retains every admitted JPEG from the existing capture path,
before ARGOS overlays, at the actual image-receive cadence. It does not open a
second camera or encode a second JPEG. The camera's own OSD remains in the image;
these are capture JPEGs, not uncompressed sensor data. The older visual replay
archive remains a separate, sampled format capped at 10 Hz.

## Launch with an existing offline configuration

Install dependencies and prepare the verified model and saved configuration
before going offline. The launchers read local files and verify model hashes;
they do not download missing weights. See [ARGOS FLY](argos-fly.md) and
[ARGOS DST](argos-distance.md) for configuration and model preparation.

For yaw-only operation, using the path of the saved FLY configuration:

```sh
.venv/bin/python -m argos.fly --config "$HOME/.config/argos/fly.json" --check
.venv/bin/python -m argos.fly --config "$HOME/.config/argos/fly.json"
```

Alternatively, use the separately prepared DST configuration:

```sh
.venv/bin/python -m argos.distance --config "$HOME/.config/argos/distance.json" --check
.venv/bin/python -m argos.distance --config "$HOME/.config/argos/distance.json"
```

Run one launcher at a time. `--check` validates local artifacts without opening
the camera or radio. The console is available on the local machine, normally
port 8080; a saved `port` or launcher `--port` changes that port. Browser assets
are served locally. The launcher's `--state-dir` selects its run-manifest and
diagnostic-log directory; it does not move the take archives described below.

The updated host parser must be present **before installing the matching Lua
script**: `scripts/edgetx/ArgFly.lua` for FLY or `scripts/edgetx/ArgDst.lua` for
DST. These scripts add optional `AP1` observations. Old scripts do not provide
numeric pilot samples; the existing V3 greeting alone does not prove AP1 support
or which script is installed. Local hashes describe local artifacts, not Pocket
SD-card readback. This document describes software capabilities, not a completed
radio installation or receiver validation.

## Start, inspect and stop a take

With the selected launcher already running and a recent physical-camera image,
use a second terminal:

```sh
.venv/bin/python -m argos.filming start --port 8080 --label "take-01-yaw"
.venv/bin/python -m argos.filming status --port 8080
.venv/bin/python -m argos.filming stop --port 8080
```

The CLI connects only to the local console. Start requests visual replay and
filming capture together. A label is optional, at most 80 characters, with no
control characters; it is metadata, not a filename. Each successful Start gets
a new recording ID. Use one Start/Stop pair per battery or take.

Stop closes admission and lets the writers finish already accepted data. The
CLI waits briefly for finalization; if it still reports `finalizing`, poll
`status` before exporting or starting another take. Inspect the filming and
camera results as well as the native journal: a completed native journal does
not establish that its camera or event sidecars are complete. `complete: false`,
nonzero drop/failure counters, an empty camera archive or unavailable pilot
samples identify missing evidence.

`pilot_required` is true when a radio service is attached. Such a take cannot
report `filming.complete: true` with zero accepted pilot samples;
`pilot_data_available` and `pilot_samples` expose that condition separately.
Completeness describes retained data, not uninterrupted camera reception or
continuous assistance throughout a flight.

The CLI uses a two-second timeout per HTTP request and waits up to five seconds
after Stop for writer status. Exit code 0 can still mean finalization is pending
or the take contains reported losses; read the status. HTTP/client failures or
a component in `error` return 1; interruption returns 130. Mutations are not
automatically retried. If a Start/Stop request times out, inspect `status` before
retrying because the console may already have applied it.

The CLI prints the recording ID and filming directory. By default takes are
stored under `~/.local/share/argos/recordings/`, or under
`$XDG_DATA_HOME/argos/recordings/` when `XDG_DATA_HOME` is an absolute path.
FLY and DST use this same default recording location.

The browser's recording panel also starts/stops captures. With an integrated
FLY/DST service, checking visual recording enables the filming sidecar by
default. The optional Demo view presents target, mode, per-axis status and pilot
sticks; its display does not become part of the saved camera JPEGs.

## Local recording API

The CLI uses the existing console API. POST requests require
`Content-Type: application/json` and an `Origin` equal to the console's base URL.
For example, when using `http://localhost:8080`, the Origin must be exactly
`http://localhost:8080`.

| Request | Body or result |
| --- | --- |
| `POST /api/recordings/start` | `{"include_visual":true,"include_filming":true,"label":"take-01"}` |
| `POST /api/recordings/stop` | `{}` |
| `GET /api/state` | Current status under `recording`, including `visual` and `filming`; radio observations under `yaw_assist` when enabled |

Filming requires `include_visual: true` and a configured physical camera.
Start requires a recent selected camera image or an open configured telemetry
link, no active recording and no previous writer still finalizing. A recording
does not require MAVLink when a recent camera image is available. Unknown fields
or invalid values return 422; conflicting recording state returns 409. Failed
recording creation returns an error rather than overwriting an existing take.

## Files and timestamps

For recording ID `TAKE_ID`, the directory contains:

```text
TAKE_ID.jsonl
TAKE_ID.visual.sqlite3
TAKE_ID.flight/
  manifest.json
  camera.mjpeg
  camera.frames.jsonl
  camera.json
  events.jsonl
```

| File | Contents |
| --- | --- |
| `TAKE_ID.jsonl` | Existing native journal and capture context; MAVLink reception if configured |
| `TAKE_ID.visual.sqlite3` | Existing sampled visual replay and console events |
| `manifest.json` | `argos.filming`, `schema_version: 1`; recording/run IDs, label, context, related files, writer status and loss counts |
| `camera.mjpeg` | Exact concatenation of the retained capture JPEG bytes; no embedded presentation timing |
| `camera.frames.jsonl` | One `schema: 1` index row per written image, with offset/length and acquisition provenance |
| `camera.json` | Camera format, time interval, sources, bounds, observed cadence, written/drop counts and finalization status |
| `events.jsonl` | `schema_version: 1` radio-status and vision observations belonging to this recording |

Camera index rows contain `session_id`, zero-based `frame`, camera `sequence`,
`source_id`, `source`, `endpoint`, `width`, `height`, `received_at`, `elapsed_s`,
`source_stamp`, `offset` and `size_bytes`. A reopened camera gets a new source
identity even if its device path is unchanged. Each offset/length identifies
one original JPEG. The index, not a player's assumed MJPEG frame rate, is the
timing authority.

Camera `received_at` and event `at` are monotonic seconds in the console run;
their `elapsed_s` fields are relative to the recording's `started_at`.
`image_received_at`, `image_elapsed_s`, `video_id` and `frame_sequence` join a
vision observation to its camera image. A source timestamp, when available,
belongs to that source's clock and does not replace local receipt time.

Radio `status.host_monotonic_at` and `pilot_sample.received_at` use the bridge's
host-monotonic clock. Each radio event includes a `clock_anchor` with
`console_before`, `host_monotonic_at` and `console_after`: the bridge-clock
reading occurred between those two console readings. Use this bracket when
joining radio timestamps to camera time; do not subtract the two clock domains
directly. A repeated latest pilot sample keeps its original receipt timestamp.
These are host observations, not camera exposure, FC time or measured physical
response times.

## Pilot samples and assistance evidence

`AP1` is an optional, bounded diagnostic message from the existing Pocket USB
stream. Its fields are:

```text
AP1 SESSION GENERATION TICKET ACK FLAGS ROLL PITCH THROTTLE YAW LUA_YAW LUA_PITCH
```

`FLAGS` packs radio state, cause, selected mode, yaw phase, pitch phase, yaw
validity and pitch validity. The host accepts a sample only when those fields
match the current radio session/status; repeated tickets do not refresh it.
Malformed observations are not command input.

The four `sticks` values are calibrated, pre-mix EdgeTX sources `ail`, `ele`,
`thr` and `rud`, recorded as integers from -1024 to 1024. They are not final
channel PWM values. `lua_outputs` contains the script's reported yaw/pitch
values and separate validity flags. It is not native mixer readback, FC
telemetry or proof that the aircraft applied a correction.

AP1 emission is capped at 10 Hz. The host's radio snapshots are also sampled
at about 10 Hz, while command delivery can reach 20 Hz. `events.jsonl` therefore
does not contain every serial write or every individual ACK. Counts, latest
requests and accepted observations remain useful evidence, with this sampling
limit. Missing AP1 support or USB loss yields unavailable/stale pilot evidence,
not zero-valued sticks.

The display considers a correlated pilot report fresh for at most 0.35 s.
Per-axis phase and validity determine manual, waiting, paused or assisted
status. Radio state `A` alone is insufficient: DST can have one axis manually
overridden while the other remains eligible for assistance.

## Bounds and incomplete captures

The camera writer has these default bounds:

| Resource | Bound |
| --- | --- |
| Capture interval | 1,800 s |
| MJPEG payload | 2 GiB |
| Index rows | 216,000; at most 8,192 bytes per row |
| Queued and currently written images | 128 images and 64 MiB |
| Filming event queue, including the event being written | 4 MiB |
| Filming event file | 128 MiB and 200,000 events |

The byte limit for MJPEG excludes its separately bounded index and manifests.
The native journal and visual replay retain their own independent limits.
Queue pressure drops incoming data and increments explicit counters; producer
threads do not wait for disk space in a queue. Camera payload, frame-count or
duration limits end camera admission. Disk errors remain visible in status.
Recording failure does not change guidance or flight authority.

Camera status distinguishes `recording`, `finalizing`, `complete` and `error`.
The `complete` state means the writer finalized; inspect its boolean `complete`,
`reason`, `empty_capture`, `dropped_frames` and `discarded_error` to assess the
evidence. `observed_fps` is calculated from retained receive timestamps. It is
an observed rate, not a promise of camera FPS or a measure of inference speed.
There is no replacement image fabricated when the source disconnects; gaps
remain visible in the timestamps.

## Export an editing video offline

Export requires the console's Pillow dependency and an installed FFmpeg with
`libx264`. FFmpeg is an optional offline export tool; capture itself does not
require it. Use the finalized filming directory printed by the CLI and a new
output path outside that directory:

```sh
mkdir -p exports
.venv/bin/python -m argos.camera_export \
  "$HOME/.local/share/argos/recordings/TAKE_ID.flight" \
  exports/take-01.mp4
```

The exporter writes `take-01.mp4` and `take-01.mp4.json`, refuses to overwrite
either, and leaves the source archive unchanged. It validates indexed JPEGs,
file sizes and timestamps, encodes H.264 with variable frame timing, then
decodes the result to verify frame count and presentation timestamps. Odd image
dimensions are padded to even dimensions. The MP4 is an editing copy; the
original JPEGs remain in the archive.

Recorded losses or a stop at a capture limit require explicit `--allow-drops`
(alias `--allow-incomplete`) after inspecting the capture. That flag does not
repair missing footage or allow writer errors. Receive gaps
hold the preceding image until the next retained image; there is no invented
intermediate camera frame. The video starts at the first retained image and
ends after its last encoded image, without filling an unobserved prefix/tail.
The JSON sidecar preserves the recording-relative offset, original timestamps,
reported gaps and SHA-256 hashes of the inputs and export. Hashes establish
export provenance, not authenticity before export.

This exporter requires a single source identity and image size, and increasing
receive times at microsecond precision. It rejects source/dimension changes
with an explicit error; automatic splitting is not implemented. Optional flags
are `--ffmpeg /path/to/ffmpeg`, `--gap-threshold 0.25` and `--timeout 3600`.
The gap threshold controls reporting, not timing or image reconstruction.
