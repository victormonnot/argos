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

### Comparing short detection gaps — September 8, 2026

Two baseline flights reproduced the reported interruption with confidence 0.5
and a 350 ms neutral pause. The first reached about 88.5 seconds of framing;
the higher-takeoff attempt stopped after about 3.4 seconds. Recorded image
metadata identified recoveries on the same ID after about 0.401 and 0.449 seconds.
Visual inspection found the selected person still present in the failure images.

The offline comparison ran the actual framing lifecycle over the same recorded
observations for nine combinations: continuation confidence 0.5/0.45/0.4 and
pause 0.35/0.6/0.8 seconds. Engagement confidence stayed 0.5 in every case.

| Continuation confidence / pause | Short-flight gap | Higher-flight gap |
| --- | --- | --- |
| 0.5 / 0.35 s (baseline) | Takeover | Takeover |
| 0.45 / 0.35 s | Takeover | Takeover |
| 0.4 / 0.35 s | Recovery | Takeover |
| 0.5 / 0.6 s (selected) | Recovery | Recovery |
| 0.5 / 0.8 s | Recovery | Recovery |

The two lowest higher-flight scores were 0.384537 and 0.366753, explaining why
lowering continuation confidence to 0.4 alone did not resolve that example.
The selected change therefore keeps confidence at **0.5** and extends only the
neutral pause to **600 ms**. Staleness remains 450 ms, tracker identity rules are
unchanged, and the subsequent manual-takeover deadline remains two seconds.

The [recorded fixtures and instructions](../examples/data/framing_dropout/README.md)
make the comparison reproducible without starting a simulator or sending flight
commands. These are fixed-path admission replays, not alternative simulated
flights or detector accuracy benchmarks. Availability timestamps are collector
observations; measured poll gaps and execution-phase uncertainty remain relevant
near deadlines. Both fixture windows end at their first recovery before manual
intervention, and never automatically restart a lost engagement.

In the live candidate's higher-takeoff trial, framing stayed engaged for about
**63.5 seconds**, including eight recovered neutral pauses. Recorded recovered
pause durations were about 0.21–0.45 seconds, with approximately 50 ms sampling
uncertainty. All 66 paused state samples had zero derived axes. A later gap still
exceeded the 600 ms deadline; the operator script requested Manual/Land and
confirmed disarming/release. The planned 90-second uninterrupted trial **did not
pass**. Several empty analyzed images followed the final low-confidence result;
a new track ID appeared afterwards. Longer pauses do not provide identity
continuity across that kind of loss.

The candidate's short-takeoff trial also failed its 90-second target: server
timestamps place engagement at **11.71–11.76 seconds** before takeover. One pause
recovered on the same ID after about 0.20 seconds; the final sequence contained
three empty results and then a new ID. All 16 paused state samples had zero
derived axes. Manual/Land, disarming and release were observed again. Across
these two candidate trials, all **82 sampled paused states** had neutral axes;
neither trial completed 90 seconds. Visual inspection of an empty-result image
from each final gap found the person still present. Detection continuity and
track identity remain limitations; extending the pause does not repair them.

The live flights started from different vehicle states, so their durations are
not a controlled speedup or reliability ratio. The paired replay establishes
recoverable short gaps; the flight establishes actual same-ID recovery while
commands are neutral. Reliable long-duration following remains unvalidated.

Focused verification passed **319 Python tests** covering framing lifecycle,
flight control, API ordering, interruption diagnostics, vehicle parameters,
guidance and the offline evaluator. These checks preserve the fixed pause
deadline, independent image freshness, manual priority and subsequent two-second
takeover deadline. This is not a new repository-wide or browser regression run;
the live trials above exercised the actual browser interface. All eight existing
user recordings retained identical byte sizes and SHA-256 hashes. Both consoles
were receiving camera and telemetry; the manual vehicle was left disarmed with
control released and recording idle.

A later idle check found both simulation feeds stale while host RAM and swap
were exhausted; the manual Gazebo process had grown to about 8.7 GiB resident
memory. Restarting only the idle manual simulation restored both feeds and more
than 8 GiB of available memory. The cause of that memory growth remains
uninvestigated; long-duration service operation is not established by the short
flight tests above.

## Optional detector profiles — September 9, 2026

The optional [YOLOX-S 640 profile](vision.md#model-and-data-flow) was compared
offline with the existing YOLOX-Tiny 416 profile on **21 saved camera JPEGs**.
The images came from four September 8 failure neighborhoods: the short and
higher baseline flights, and both later 600 ms-pause candidate flights. They
include eight images where Tiny published no detection, five with a published
box below the 0.5 framing threshold, and eight adjacent detections above it. This is
a deliberately selected difficult sample, not a representative accuracy set.

Both models used the same original pixels, preprocessing convention, person
publication threshold of 0.35, NMS threshold of 0.45 and maximum of 16 boxes.
S changes both network capacity and input resolution; this is not a resolution-only
experiment. Tiny re-inference reproduced all recorded boxes and confidence
values exactly. Neither weights nor thresholds were tuned for these images.

| Model | Images with a box ≥ 0.35 | Images with a box ≥ 0.5 | Images with multiple boxes | Total median / p95 / maximum |
| --- | ---: | ---: | ---: | --- |
| Tiny416 | 13 / 21 | 8 / 21 | 0 | 45.2 / 54.3 / 68.9 ms |
| S640 | 21 / 21 | 21 / 21 | 0 | 166.9 / 192.2 / 256.7 ms |

These wall times were measured on the ground computer with OpenCV 5.0.0, CPU
backend and two threads, while the simulator remained running and grounded.
The models ran serially with two warmup forwards followed by three timed passes
per image: 63 timed observations per model. Total time includes JPEG decoding,
preprocessing, network inference and box decoding; it excludes disk access,
model loading, worker IPC and event-loop publication. The p95 uses sorted sample
index `floor(0.95 × (n − 1))`. Every repeated output was identical. These timings
are broader than the network-only `inference_ms` displayed in the interface.

All 21 original images were visually inspected against the S box coordinates.
Each contained one visible full person, and the S boxes plausibly covered the
person from head through feet; no obviously unrelated or body-part-only box was
found. S confidence ranged from 0.782 to 0.889. This is qualitative pixel review,
without annotated boxes, ground-truth IoU, calibrated confidence or a measured
precision/recall score. Pose-dependent box changes remain relevant to framing.

A separate **nine-image September 9 manual-yaw subset** contained five fully
visible person images during rapid rightward image motion, three visually empty
images after the person left the right edge, and one partially visible person
at that edge. Both models returned six detections and three empty results, with
no extra person boxes. Both detected the partial person, so clipping checks
remain necessary. The S total median / p95 / maximum was **174.2 / 200.9 /
223.6 ms** over 27 timed runs. The fully visible detections accompanied changing
track IDs under rapid image displacement; changing the detector alone does not
repair that association failure. Three empty images do not establish a general
false-positive rate.

The model files' exact sizes, hashes, upstream source and explicit setup/launch
commands are documented in [vision.md](vision.md). The saved JPEGs and raw local
benchmark outputs are not included in the repository, so these numbers document
this evaluation rather than a clone-runnable image benchmark. The checked-in
tests cover both export geometries, model integrity, variant selection and the
worker/configuration path without downloading weights or running inference.

**Tiny remains the default.** S is an explicit option for further ground-computer
evaluation. Its p95 is close to the 200 ms interval of a five-Hz pipeline, and
some calls take longer. One pending image prevents an accumulating queue, but
camera age, IPC and scheduling still contribute to the 450 ms framing limit.
This saved-image result does not establish live S freshness, continuous framing,
identity preservation through crossings or real outdoor performance. It cannot
prove an alternative successful flight: different commands would change later
camera images. No S flight validation is claimed by this offline comparison.

### S CPU thread comparison

A subsequent bounded comparison reran the same verified S model and original
21 JPEGs with two and four OpenCV CPU threads. This measurement was separate
from the table above: the load now included a grounded simulator and the active
S worker in the console. The comparison ran after the current capture finished,
with the vehicle disarmed and control released. No flight commands were sent.

The host reports an Intel Core i5-12400F, one socket, six cores and twelve logical
CPUs with two hardware threads per core; `lscpu` also reports Microsoft full
virtualization. Each setting used two warmups and 63 timed calls, preserving the
same total-time definition and p95 calculation as the earlier comparison.
The two-thread run preceded the four-thread run; changing background load and
run order remain potential timing influences.

| S thread limit | Total median / p95 / maximum | Images with a box ≥ 0.5 |
| --- | --- | ---: |
| 2 | 188.1 / 213.6 / 260.7 ms | 21 / 21 |
| 4 | 123.5 / 132.2 / 141.2 ms | 21 / 21 |

Four threads reduced median total time by about 34% and p95 by about 38% in this
comparison, with a lower observed maximum. Six threads were not tested because
four already met the investigation's target of median below 130 ms and p95 below
160 ms. Those targets guide this benchmark; they are not flight admission rules
or guaranteed execution bounds.

The console and simulation launcher therefore expose the explicit
`--vision-threads` integer option from 1 to 6, preserving the default of **2**.
The configured limit appears in vision status. Frame freshness, detection
thresholds, tracker gates and takeover deadlines are unchanged. Lower network
cost leaves more room for camera receipt age, IPC and event-loop scheduling;
only a subsequent live test can establish the resulting complete image age.

### Live S framing checks

The real console, camera worker, Gazebo and ArduPilot SITL were exercised through
the shipped browser controls, with no mocked detections or injected flight state.
The simulation profile still disabled GPS. The 0.5 framing confidence gate,
450 ms image-age limit, 600 ms neutral detection pause, association gates and
two-second takeover deadline were unchanged.

With S and **two threads**, one engagement was accepted and then latched takeover
for `Selected target image is stale` after approximately 0.23–0.28 seconds,
bounded by the adjacent server snapshots. The lost observation was 468.3 ms old
and still contained the selected ID with confidence 0.855. The requested 60-second
window failed. Manual stop, Land, disarming and release were verified.

Across the complete 115-second capture, 46 of 2,294 state samples (2.0%) contained
an analyzed image older than 450 ms. Sampled age p50 / p95 / maximum was
337.0 / 436.4 / 514.0 ms, with about 4.81 processed images per second. All 554
unique images were first observed before 450 ms: images aged beyond the limit
between subsequent results. `vision.state == recent` alone is insufficient here,
since visual display uses a one-second limit and framing uses 450 ms.

After selecting **four threads**, a separate flight used a 6.5-second held climb,
released manual input, selected the detected person and engaged framing. It
completed **60.010 seconds** of DOM-observed active framing without an observed
pause, changed selected ID or loss of authority. Explicit Manual, Land, observed
disarming and release completed afterward. DOM sampling and server capture are
observations of the loop, not hard execution-time guarantees.

The complete four-thread capture contained 2,294 state samples and 560 unique
images. Sampled image age p50 / p95 / maximum was **254.3 / 362.5 / 410.8 ms**,
with no sampled age over 450 ms and about 4.86 processed images per second.
The 1,199 active states retained ID 1; all 1,095 non-active states had zero framing
outputs. During the active interval, normalized horizontal error ranged from
−0.180 to +0.286 and apparent height/reference from 0.801 to 1.259. Passing the
continuity window does not demonstrate precise centering or distance convergence.

A second four-thread flight, after an eight-second held climb, **failed the
requested 90-second window after 10.331 seconds of DOM-observed framing**.
The detector emitted two nested boxes on one visible person: selected ID 1 at
0.834 confidence and a smaller ID 2 box at 0.357. The controller latched
`Framing requires exactly one visible person` on a 106.1 ms-old image. Visual
inspection confirmed one person in that image. Manual stop, Land, disarming and
release were again verified. Duplicate suppression and identity continuity remain
open; no detection was discarded or admission gate weakened for this trial.

The changed thread limit followed a failed live run and a separate timing
comparison; neither freshness nor detection admission was relaxed to obtain this
result. These independent flights are not paired accuracy trials: simulator
phase, vehicle motion and resulting pixels differ. They establish a limited
working configuration on this ground computer, not reliable following outdoors.

### Remaining association limitation

A separate manual-yaw trial reproduced four consecutive ID changes while the
same person remained detected above 0.5 confidence. The image moved horizontally
by 43–66 pixels between detections, beyond the existing nearby-association gate;
framing had not engaged. The person subsequently left the camera image, so the
later empty results were legitimate. Changing detector capacity cannot repair
all geometric association failures or supply an observation outside the image.

Exploratory background feature matching and foreground feature tracking were
not integrated. Background matches lacked spatial support near the person; the
foreground prototype met its complete checks on only one of four broken pairs.
Real different-person replacements and crossings were not evaluated. A future
tracker requires those tests and exact previous/current image correspondence;
no predicted box or silent ID reassignment was added in this change.


For this profile/provenance change, **252 focused Python tests** passed on the
simulation host and **25 browser vision tests** passed on macOS Chromium. They
cover model integrity and geometry, option forwarding, worker/source lifecycle,
framing integration, optional status rendering and startup manifests. These
focused checks are not a new repository-wide validation claim.
