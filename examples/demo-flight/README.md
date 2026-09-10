# Recorded flight demo

Explore a real archived **Gazebo simulation session** in the normal ARGOS web
interface. Play, pause, seek backwards, inspect matched detections, and compare
the recorded controls with telemetry and messages. This is a 36.58-second
recording: replay does not fly a drone or change the recorded outcome.

No Gazebo, ArduPilot SITL, camera, radio, vision model, OpenCV, or second computer
is needed to replay it. A source checkout and Python 3.11+ with the `console`
and `mavlink` extras provide the replay dependencies. This launch was validated
on Ubuntu 24.04 with Python 3.12; other operating-system combinations were not
newly validated. The normal console serves the included files; there is no
separate imitation of the application.

## Open it

From the repository root, in your activated Python environment:

```bash
python -m pip install -e '.[console,mavlink]'
python -m argos.console --recordings-dir examples/demo-flight --port 8082
```

Open **http://127.0.0.1:8082**, choose **Sessions**, then open
**Recording 05aa147d**. **Flight replay** opens. Press **Play** or **Next** to reach
the first captured image; capture begins slightly after the journal start.
Use the time cursor to explore. **Measurements**, **MAVLink messages**, and
**Analysis** expose the actual recorded telemetry. The live Observation page has
no connected sources in this launch, so waiting/unavailable live measurements
are expected. It does not present archived data as live telemetry.

Port 8082 keeps this process separate from a live control console on 8081. Stop
the replay server with Ctrl+C. The recording files are read by the replay; no
capture, control claim, camera, or telemetry endpoint is configured by this
command. The normal source configuration controls remain available if you later
choose to connect a source yourself.

## A short walkthrough

Times below are offsets within the recording. The manifest retains the exact
original event timestamps and references; the rounded times are navigation aids.
Requests describe actions accepted by the service. Vehicle observations and
MAVLink acknowledgements remain separate evidence in the recorded data.

| Time | What to inspect |
| --- | --- |
| 0.19 s | Take control request, followed by preparation of AltHold. |
| 4.10 s | Arm request. Watch the subsequent manual ascent in the video. |
| 13.29 s | Person selected; Engage framing follows at 13.36 s. |
| 17.40 s | Closer increases the target's requested apparent image height. |
| 21.43 s | Farther restores the previous apparent-height reference. |
| 25.58 s | Return to manual control, followed by Land at 25.63 s. |
| 36.25 s | A sampled vehicle state reports disarmed; control is released at 36.50 s. |

Pause around 18–22 seconds to compare the target rectangle and framing state,
then seek back to before engagement. Boxes belong to their captured JPEG and
become available at their recorded processing time. The cursor never reruns the
detector. Closer/Farther use apparent image height; these values are not a
calibrated distance in metres.

## What was captured

- Browser-driven QA used the normal ARGOS control endpoints with ArduCopter SITL,
  the `inspection` scene and the forward Gazebo camera on 10 September 2026.
- YOLOX-S ran at capture time with four vision threads and a maximum 5 Hz rate.
  Replay only reads its archived results.
- The fixture contains **1,970 MAVLink receptions, 177 JPEGs at 640 × 480,
  353 sampled control states and 24 events**, with no reported visual drops.
- These are the **unaltered original JSONL and SQLite files**, totaling
  5,920,234 bytes. Their SHA-256 digests and capture identity are in
  [manifest.json](manifest.json). Keep both files together with the same ID.
- Camera timing is local receipt/availability timing, not exposure-level
  synchronization. Sampled control states are not a complete transmitted-command
  log. The recorded simulator coordinates are simulation data, not a real flight
  location or GPS-free navigation validation.

The short session retains ordinary diagnostic history. Its first control samples
include a denied arm command from before capture began; this session subsequently
arms successfully. MAVLink `STATUSTEXT` also reports **GCS Failsafe Cleared** at
0.518 s and **GCS Failsafe** at 29.525 s, after the Land request at 25.630 s.
The archive preserves these messages without inferring their cause. Inspect them
in **MAVLink messages** alongside the later **Disarming motors** message. This
fixture is a reproducible example to inspect, not a claim of flawless flight or real-world
tracking performance. Historical sampled-event wording has not been rewritten.

Verify the files, their embedded integrity checks, capture binding, every JPEG's
digest and decoded dimensions, and the chapter evidence with:

```bash
python examples/verify_demo_flight.py
```

This verifier opens no device or network source. The hashes check consistency with this repository's manifest.

## Rendered asset attribution

The walking person is the unmodified skin and skeletal animation from
[Mingfei's actor, Gazebo Fuel version 1](https://fuel.gazebosim.org/1.0/Mingfei/models/actor/1),
declared **Creative Commons Attribution 4.0 International**
([license](https://creativecommons.org/licenses/by/4.0/)). ARGOS supplies the
walking trajectory. The recorded camera images include this rendered actor;
the fixture does not redistribute its mesh. The original asset checksum and
attribution are also retained in the [scene guide](../gazebo/README.md#asset-provenance).

The inspection yard geometry and materials were authored for ARGOS under the
repository's MIT license. Existing ArduPilot Gazebo vehicle, camera, and world
asset provenance is documented in the [simulation guide](../../docs/sitl-observation.md).
Those underlying asset licenses are unchanged; this fixture does not relabel all
rendered third-party content as MIT.

To actually pilot the simulation and create another flight, follow the
[web control guide](../../docs/web-control.md). That separate experience runs
Gazebo and SITL and reacts to new control inputs.
