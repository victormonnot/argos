# Environment and validation

The published V1 corresponds to package version **0.1.0**, with passive
observation and recorded-session analysis. The working tree additionally contains
the opt-in [manual web flight](web-control.md) and [camera perception](vision.md)
milestones verified below; no new release is implied. This page separates automated, simulator and hardware evidence.

## Automated checks

[CI](../.github/workflows/ci.yml) defines three jobs on Ubuntu 24.04:

| Job | What it checks |
| --- | --- |
| Python 3.12 | The pytest suite: contracts, simulation, local transports, measurement validation, captures, archives and API. The bundled SITL example is also verified. |
| Browser, Node.js 22 | Playwright workflows in Chromium, using a local file server and test responses. No dependency on a simulator or an existing console. |
| Installed package, Python 3.12 | Wheel build, installation in a separate virtual environment, CLI, HTML/CSS/JS, fonts and state with no source configured. |

The wheel job runs `scripts/smoke_installed.py` with `python -I` and checks that
the module comes from `site-packages`. This detects issues such as missing
packaged assets that an editable installation could hide. The smoke test makes
in-memory HTTP requests without opening a network server, camera or MAVLink link.

Transport tests use localhost UDP/TCP and a POSIX serial pseudo-terminal.
Simulated faults remain in tests. They do not replace validation of radio
hardware, a camera driver or an actual flight.

## Installation and versions

Local preparation verified a wheel build and a non-editable installation in a
fresh Python 3.12 virtual environment, separate from the development environment.
Imports, `python -m argos.console --help`, web assets and `pip check` all passed.
The package includes frontend assets and font licenses.

Direct MAVLink, console and plotting dependencies are pinned in
[pyproject.toml](../pyproject.toml). NumPy, pytest, the build tool and transitive
dependencies are still resolved by pip. The repository therefore does not
promise an identical binary environment on every installation. The tested
environment is Ubuntu 24.04 x86-64/Python 3.12; declaring Python ≥3.11 does not
amount to validating a full platform matrix.

## SITL and camera trial

The ground trial on September 7, 2026 received the Gazebo sensor and ArduPilot
TCP stream simultaneously. The [guide](sitl-observation.md) provides both full
revisions, model changes and exact parameters. The reference binary identifies
itself as ArduCopter-ARGOS 4.8.0-dev `8927564c`; the observed Gazebo libraries are
Sim 8.13.0, Transport 13.5.0 and Msgs 10.3.2.

The supplied patch was verified against copies of the upstream files at the
documented revision. The setup guide follows official instructions; a complete
system installation and rebuild from a fresh machine were not repeated for this
V1. Headless rendering depends on the machine's graphics support even when no
Gazebo window is open.

The [example recording](../examples/demo/README.md) is a separate historical
extract. Its frames and checksum are verified; the older format does not record
the exact UTC date, firmware or capture configuration. The manifest does not
infer those details from the current trial.

## Manual web flight trial — September 8, 2026

The opt-in simulation milestone was checked on the same Ubuntu 24.04/Python
3.12 host and pinned ArduPilot/Gazebo installation:

- **1,161 Python tests passed**, including lease deadlines, command evidence,
  input bounds, source restrictions and passive recovery after link loss. The
  final profile uses the pinned firmware’s renamed speed/tilt parameters in m/s
  and degrees; their received values are required before arming.
- **32 Chromium browser tests passed** (17 existing and 15 new), including real
  Chromium touch pointer events, combined axes, capture loss, hidden tabs and
  responsive layouts. Laptop, tablet and phone layouts were visually inspected.
- A newly built wheel was installed in a separate clean virtual environment.
  Isolated imports, CLI help, packaged assets (including the control panel),
  passive initial state and `pip check` passed.
- An isolated Gazebo/SITL flight used the supplied GPS-free profile with normal
  arming checks. Through the actual HTTP service, claim, AltHold preparation,
  arming, climb, yaw input, Land mode and automatic disarming were observed.
  Both GPS receivers and compass yaw use were disabled; no flow, marker,
  rangefinder or visual-position source supplied navigation.
- The real browser then exercised the complete path without mocked endpoints:
  touchscreen input generated via Chromium's device protocol held **Climb**,
  produced a climb in reported barometric local altitude, released
  to neutral, requested Land and reached confirmed disarming. The Gazebo camera
  remained visible. This is not a physical-tablet test.
- Stopping browser input during a simulated flight expired the lease, requested
  Land and ended with observed landing/disarming; no browser action regranted
  control automatically.
- Suspending the console process during another flight stopped its transmissions.
  A separate receive-only MAVLink connection observed Land while the console was
  still suspended, confirming ArduPilot's GCS failsafe without a backend landing
  request. After resuming reception, landing and disarming were confirmed.

The simulator ran below real-time speed while another Gazebo instance was active.
Wall-clock button duration is therefore not a calibrated flight-time measurement.
The final complete profile was read back from SITL: every configured name was
present and its value matched, including the speed/tilt settings.
The recorded altitude is autopilot telemetry, not an independent accuracy
measurement. These trials demonstrate simulated manual control, not reliable
position holding, target following, or physical-aircraft readiness.

## Stabilize manual-throttle trial — September 8, 2026

The next manual-control milestone used the same pinned installation, with a
fresh launch of the supplied GPS-free profile:

- **1,226 Python tests passed**, including explicit preparation per lease,
  ground-only mode selection, zero throttle before arming, input validation,
  source recovery and the single manual-input handoff to Land. Tests also cover
  refused, missing and failed Land commands: ongoing browser updates cannot
  keep GCS heartbeat running and suppress the autopilot fallback.
- **46 Chromium browser tests passed**, including retained manual gas after
  releasing a direction, real Chromium touch events combining gas adjustment
  with a held direction, ground mode selection and lifecycle resets. Laptop,
  tablet and phone layouts were inspected. These are not physical-tablet tests.
- A newly built wheel passed installation into a fresh environment, isolated
  CLI/import checks, packaged mode/throttle assets, passive default-state checks
  and `pip check`. The offline recording example verified its 58 frames.
- Through the real browser and HTTP service, Stabilize preparation and arming
  were confirmed at **0% manual gas**. A touch-set **57% pilot input** produced
  a climb in reported local altitude; releasing a yaw control retained that gas.
  Received RC channel telemetry also reflected the throttle input. Land and
  automatic disarming completed, followed by a new lease and successful AltHold
  climb/landing. No mocked control endpoint was used for these flights.
- In another Stabilize flight, stopping browser inputs expired the lease and
  ended in Land and confirmed disarming, with stored throttle reset to zero.
- Suspending the console during a separate Stabilize flight stopped all of its
  transmissions. A receive-only MAVLink connection observed Land while the
  console was still suspended; reception after resuming confirmed landing and
  disarming. The observer sent no GCS heartbeat or pilot commands.
- All **39 configured profile parameters** were read back and matched. The
  profile now sets RC override expiry to **3 s**, after the **2 s** GCS Land
  timeout, so loss of the service does not first restore low underlying RC gas.
  Explicit Land also stops GCS heartbeat until a new ground preparation.

Throttle percentage represents normalized pilot input, not measured thrust or
a calibrated vertical speed. Stabilize does not regulate altitude; neither mode
holds horizontal position. These trials use autopilot telemetry and below-real-time
simulation, not an independent position-accuracy measurement. They do not validate
in-flight mode transitions, physical flight, visual following or a metric range
estimate. No GPS, downward flow, marker or external visual-position input was used
for flight control.

## English interface — September 8, 2026

The interface now uses English throughout Observation, Flight controls, Live
MAVLink, Sessions, source settings, accessible names and service-generated
status/error messages. Numbers and dates use the explicit `en-US` locale.
Autopilot payloads and existing recordings retain their original content.

The complete **1,226 Python tests and 46 Chromium browser tests passed** with
English expectations. Existing control, touch, lifecycle, source-recovery and
recording checks were retained. Desktop, tablet and phone layouts were inspected;
a freshly built installed wheel passed its resource and passive-state smoke
checks. HTML structure and identifiers were preserved, and Python syntax-tree
comparison confirmed that changes outside documentation were string values.
No flight-control algorithm, protocol field or recording format changed, and
this language update did not repeat the earlier flight trials.

## Remaining limitations

- The V4L2 adapter exists, but it still needs validation on the chosen hardware.
- No onboard radio video transport is provided; physical camera input is local.
- Simulation tests for the other modules do not connect them to a real autopilot.
- CI does not run flights, hardware deployments or a complete Gazebo simulation.
- The first GitHub Actions result will only exist after the first push; having a
  workflow file does not mean it has already run on GitHub.

## Camera person detection and tracking — September 8, 2026

The optional [vision milestone](vision.md) was checked on the pinned Gazebo/SITL
installation, using the actual onboard camera, an ordinary animated person and
the unmodified official YOLOX-Tiny ONNX model. The person scene keeps the declared
1.2-radian camera HFOV fixed: the upstream zoom plugin otherwise changes it to
2.0 radians during startup. Only the private scene copy removes that plugin;
actor scale, detection confidence and the GPS-free flight profile are unchanged.

- **1,312 Python tests passed**, covering detector decoding, model/asset checks,
  bounded image association, process failure isolation, source replacement,
  matching JPEG/result provenance and expiry, alongside the existing suite.
- **71 Chromium tests passed**, including 25 vision tests for delayed responses,
  switching streams, invalid metadata, stale/service/decode failures, overlay
  geometry and touch layouts. Labels and camera fitting were inspected on
  desktop, tablet and phone viewport sizes. These are browser emulations, not
  physical-device trials.
- The rebuilt wheel passed isolated installation, CLI, packaged vision controls,
  passive defaults, unavailable-image responses and dependency checks. The
  offline MAVLink example still verified all 58 frames.
- A final **40-second ground sample contained 196 unique analyzed frames**.
  Every frame detected the one visible person, with one track ID throughout and
  zero ID changes. Person height ranged from 68 to 100 pixels. Median confidence
  was 0.865; camera receipt age was 159 ms median and 287 ms maximum. CPU network
  inference took 47.6 ms median and 62.9 ms maximum. These are observed values on
  one host running two Gazebo instances, not general accuracy or latency bounds.
- An actual GPS-free AltHold flight through the HTTP service, with vision active,
  completed arming, climbing past 1.7 m of reported local altitude, neutral
  vertical input, a brief yaw input, Land and confirmed disarming. All 136 status
  samples reported recent vision; 135 contained a person detection. These status
  samples are not independent image-level accuracy measurements. No input
  request failed; observed request latency was 1.69 ms median and 5.21 ms maximum.
- Terminating the inference process during a separate disarmed control lease
  produced an explicit vision error while the raw camera advanced and 39
  neutral control input requests succeeded. The optional worker failure did not
  stop MAVLink/control servicing. Automated tests also cover startup/inference
  deadlines and IPC failure containment.

The initial wide-angle trial missed small people in some frames. After fixing
the camera configuration, detections were continuous in the sampled scene, but
strict overlap-only association still changed IDs during lateral walking. A
bounded, mutually unambiguous center/size association fallback removed those
changes in both a replay of the measured boxes and the final live sample above.
No scene coordinates, marker, body-height assumption or predicted detection
filled gaps or corrected model output.

This establishes a camera-based perception baseline during manual simulated
flight. It does not establish reliable identity tracking through crossings or
occlusion, physical outdoor accuracy, automatic centering/following, position
hold or metric range. The animated actor does not physically react to the drone.
Existing journals remain received-MAVLink captures; video and detections are not
recorded or replayed. The eight existing journals were byte-identical after the
trial and deployment.

## Experimental visual framing — September 8, 2026

The opt-in [framing controller](framing.md) connects an explicitly selected
camera detection to bounded AltHold pilot inputs in the isolated GPS-free
Gazebo/SITL session. The detector, confidence thresholds, actor dimensions and
camera optics are unchanged from the vision milestone. No target coordinates,
depth, known body size or simulated position enter the framing law.

- **1,472 Python tests passed** on the supported Ubuntu/Python 3.12 environment.
  They cover image-only guidance, independent output bounds, ownership and
  delayed-intent fencing, manual priority, pause/takeover deadlines, perception
  failure isolation, parameter mapping and paced parameter discovery, alongside
  the complete existing suite. Two existing Starlette deprecation warnings remain.
- **89 browser tests passed** using local Chrome on macOS with one worker,
  including exact displayed-frame selection, touch/focus preservation, delayed
  Engage/Manual requests and explicit pause/resume. Desktop, tablet and phone
  layouts were inspected; these are browser emulations, not tablet hardware trials.
- The rebuilt wheel passed isolated installation, CLI, packaged framing assets,
  disabled-by-default state and dependency checks.
- All **45 required parameters** were received and matched before flight.
  The first attempted startup exposed ArduPilot's bounded parameter-response
  queue: only 20 of the original 45 simultaneous requests received replies.
  Keeping a small initial batch and pacing remaining/missing reads resolved this
  without changing values or bypassing the arming gate.
- An actual HTTP-controlled flight completed manual takeoff and **55.21 seconds
  of framing engagement**, including three observed neutral-pause episodes. The
  25-second initial stage and 20-second Closer stage completed. Farther ran about
  9.93 seconds before an unusable selected-target observation outlasted the pause
  and latched takeover. The script then explicitly requested Land; reported
  landing and disarming completed. The planned 65-second uninterrupted trial
  did **not** pass.
- During that flight, median absolute horizontal centering error was about
  5.3%, 5.2% and 2.2% of image width in the three stages. Vertical medians were
  about 1.2%, 2.1% and 1.2% of image height. Reported estimator altitude stayed
  between 1.02 and 1.07 m, peak pitch was 2.084 degrees and reported horizontal
  speed reached 1.273 m/s. These are sampled camera/telemetry results, not an
  independent ground-clearance measurement or guaranteed performance bounds.
- **Apparent-size regulation did not settle reliably.** Median height/reference
  ratios were 0.932, 0.959 and 0.734; the Closer stage's 5th–95th percentile range
  was 0.686–1.324. These results establish an adjustable experimental objective,
  not accurate distance keeping. The actor keeps walking during each stage.
- A separate real-browser flight used held Climb, clicked Person #4, engaged
  through the actual API and observed **8.16 seconds** of assistance before
  Manual, Land and confirmed disarming/release. Engagement's `input_seq=92`
  was acknowledged after the last nonzero manual input at sequence 77. There
  were no mocked flight responses. Current box height changed from 13.1% to
  16.0% against its 13.1% reference, again showing range-regulation limitations.
- In a separate airborne fault trial, terminating the inference worker just
  after confirmed engagement produced neutral takeover outputs. Continued
  neutral browser traffic did not acknowledge the loss: authority was revoked
  approximately **1.96 seconds after the first sampled takeover state**, then
  Land and disarming were observed without an operator Land request.

Earlier trials stopped on brief confidence/detection gaps or a changed track ID.
A dedicated camera capture contained 291 unique frames, with the same track ID
on all 289 detected frames. Both isolated missing-frame JPEGs visibly contained
an unobstructed person. This justified the explicit 350 ms **neutral-output**
pause: fresh same-ID recovery can continue engagement, with command/derivative
history reset. No old box, predicted detection or lower threshold fills the gap.
A changed ID, stale image or other invalid observation still latches takeover;
a good frame arriving after the pause deadline cannot restore assistance.

Centering is usable in the tested scene, while size regulation and perception
continuity need further work before a dependable following demonstration.
These trials do not validate outdoor following, physical flight, radio/HITL,
VIO, horizontal position hold, obstacle clearance or metric range. Received
MAVLink remains the journal format; visual commands and image history are not
replayed in Sessions. Existing user recordings were preserved.

### Interruption diagnostics follow-up — September 8, 2026

A reported early framing failure left a server error confirming takeover timeout
and an automatic Land request. The browser replaced that cause with either a
generic interrupted-control message or an expired-owner response, depending on
whether state polling or the next input request observed the revocation first.
The original detection failure was not retained and cannot be reconstructed for
that attempt.

Two additional real-browser flights on the unchanged controller each observed
40 seconds of framing after manual takeoff. Neither reproduced a full takeover.
During the higher-takeoff flight, captured images established one empty detection
and one same-ID confidence drop to 0.46984, while the person remained visibly in
the image. Fresh same-ID observations returned in about 0.2 seconds in each case,
so both neutral pauses recovered. The first sampled image ages were 0.082 and
0.073 seconds: these two gaps were not stale-image or changed-ID failures.

The diagnostic correction preserves the first takeover's cause and image
metadata, and keeps the revoked lease's cause visible after a late input error,
Land and disarming. A separate landing outcome remains visible if sending or
confirmation fails. Tracking thresholds, pause/takeover deadlines, guidance and
browser authority are unchanged. These successful short flights do not establish
that the reported intermittent loss is resolved.

Focused verification passed 295 Python checks covering flight control, framing,
API ordering, diagnostics, profiles and guidance, plus 56 browser checks covering
manual/framing controls and both interruption-response orders. These are focused
regressions, not a rerun of the previously reported repository-wide validation.

After deployment, a real-browser ground-only claim/release displayed the server's
retained interruption in both control panels. Camera, vision and vehicle receipts
were recent; the vehicle remained disarmed. All eight existing user recordings
matched their before/after byte sizes and SHA-256 hashes.
