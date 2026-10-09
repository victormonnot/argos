# Documentation

[Project overview](../README.md) · [Try a recorded flight](../README.md#try-a-recorded-flight) · [Run the tests](../README.md#verify-a-change)

Choose the part of ARGOS you want to use. Setup instructions, offline experiments
and bench checks are grouped separately below.

## Start here

| I want to… | Start with |
| --- | --- |
| Explore ARGOS without hardware | [Recorded flight](../examples/demo-flight/README.md), then the [console guide](console.md) |
| Fly in simulation | [SITL and Gazebo setup](sitl-observation.md) → [browser flight controls](web-control.md) → [visual framing](framing.md) |
| Set up camera-based yaw assistance with a Pocket radio | [ARGOS FLY](argos-fly.md) and its [radio profile](edgetx-fly-profile.md) |
| Understand why a selected person is lost | [Continuity diagnosis](continuity-diagnostics.md), then the [offline comparison and recovery tools](#vision-and-offline-experiments) |

## Console and recordings

| Guide | What it covers |
| --- | --- |
| [Console](console.md) | Camera and telemetry sources, live views, recording, replay and API |
| [Running a session](running.md) | Starting, stopping, tmux and access from another computer |
| [Recorded flight example](../examples/demo-flight/README.md) | A bundled simulator flight to replay, with a guided timeline |
| [Filming](filming.md) | Capturing physical-camera video and pilot/radio observations, then exporting them |
| [Demo view](demo-view.md) | Reading target, pilot-input and assistance status in the compact presentation |
| [Telemetry-only example](../examples/demo/README.md) | A small MAVLink recording for message and reception analysis |

## Simulation

Start with the simulator setup, then the flight-controls guide. Visual framing
adds assistance to that separate Gazebo/ArduPilot workflow.

| Guide | What it covers |
| --- | --- |
| [SITL and Gazebo setup](sitl-observation.md) | Dependencies, reference revisions, camera and telemetry connection |
| [Browser flight controls](web-control.md) | Manual simulated flight, flight modes and input handling |
| [Visual framing](framing.md) | Selecting a person and controlling their position and apparent size in the image |
| [Gazebo scenes](../examples/gazebo/README.md) | Inspection scene, walking-person assets and attribution |
| [Virtual-radio bench](radio-bench.md) | An automated simulated flight with pilot inputs, assistance and source-loss scenarios |

## Physical camera and radio

ARGOS FLY is the integrated yaw-assistance workflow. ARGOS DST is the separate
experimental pitch/yaw workflow. Follow each guide's configuration and receiver
checks for the setup you are using.

| Guide | What it covers |
| --- | --- |
| [ARGOS FLY](argos-fly.md) | Local launch, target selection and yaw assistance through the Pocket |
| [FLY radio profile](edgetx-fly-profile.md) | Preparing, installing and inspecting the matching radio model |
| [Yaw-stream contract](edgetx-yaw-stream.md) | Host/radio protocol, command freshness and pilot takeover |
| [ARGOS DST](argos-distance.md) | Experimental pitch assistance using apparent person size, alongside yaw assistance |

For recording these sessions, see [Filming](filming.md). For individual component
checks, use the [hardware benches](#hardware-bench-checks).

## Vision and offline experiments

### Detection and models

| Guide | What it covers |
| --- | --- |
| [Person detection](vision.md) | Optional detector setup, tracking, simulation scene and physical-camera preview |
| [Custom models](custom-vision-models.md) | Preparing, comparing and explicitly loading a custom YOLOX-Nano bundle |
| [Detector comparison](vision-comparison.md) | Comparing detectors on the same saved images and viewing the results |
| [CPU thread comparison](vision-performance.md) | Measuring local inference time with different CPU thread counts |

### Selected-person continuity

These tools investigate target loss on saved recordings and controlled scenarios.
Their tracker and recovery experiments do not replace the live flight tracker.
Read them in this order:

1. [Continuity diagnosis](continuity-diagnostics.md) — inspect detection,
   association, target selection and dry control admission around a loss.
2. [Tracker comparison](tracker-comparison.md) — compare ImageTracker, ByteTrack
   and BoT-SORT with the same saved detections.
3. [Dense review and counterfactuals](continuity-validation.md) — review each
   image and test specific selection changes.
4. [Recovery prototype](recovery-prototype.md) — compare isolated appearance,
   motion and candidate-qualification policies.
5. [Multi-person qualification](multi-person-qualification.md) — exercise
   controlled synthetic cases, including wrong-person counterexamples.

## Hardware bench checks

These are separate component checks, each with its own prerequisites. Use the
[FLY](argos-fly.md) or [DST](argos-distance.md) guide for the integrated setup.

| Guide | What it covers |
| --- | --- |
| [Betaflight USB readout](betaflight-probe.md) | Reading controller identity, attitude, status and received RC channels over MSP |
| [EdgeTX USB display](edgetx-usb-display.md) | PC messages and acknowledgements on the radio screen |
| [Unused-channel mixer bench](edgetx-usb-mixer-bench.md) | A copied radio model, one unused channel, RF disabled and manual fallback |
| [Native stale-output guard](edgetx-native-guard-bench.md) | The radio-side gate and a deliberately held producer |
| [Vision-to-channel bench](edgetx-vision-bench.md) | Real-camera yaw preview connected to unused CH32 with RF disabled |
| [Disarmed RF yaw bench](edgetx-rf-yaw-bench.md) | A bounded yaw sequence received by a disarmed Betaflight controller |

## Development and reference

| Reference | What it covers |
| --- | --- |
| [Architecture](architecture.md) | Simulation and physical-radio paths, control ownership, recordings and offline experiments |
| [MAVLink transport](mavlink-transport.md) | Transport options, decoding, reception measurements and journal format |
| [Test commands](../README.md#verify-a-change) and [CI workflow](../.github/workflows/ci.yml) | Development checks and installed-package verification |
| [Verification and trial reports](validation.md) | Current test evidence and prerequisites, followed by the dated September 2026 trials |
| [Dropout fixture](../examples/data/framing_dropout/README.md) and [overlap fixture](../examples/data/framing_overlap/README.md) | Metadata used for offline framing-interruption checks |

Licenses and provenance: [ARGOS code](../LICENSE),
[bundled fonts](../argos/console/static/fonts/README.md),
[tracker code](../argos/perception/_vendor/README.md) and
[rendered demo assets](../examples/demo-flight/README.md#rendered-asset-attribution).
