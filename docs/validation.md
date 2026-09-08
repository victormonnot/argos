# Environment and validation

The published V1 corresponds to package version **0.1.0**, with passive
observation and recorded-session analysis. The working tree additionally contains
the opt-in [manual web flight](web-control.md) milestone verified below; no new
release is implied. This page separates automated, simulator and hardware evidence.

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
  touchscreen input generated via Chromium's device protocol held **Monter**,
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

## Remaining limitations

- The V4L2 adapter exists, but it still needs validation on the chosen hardware.
- No onboard radio video transport is provided; physical camera input is local.
- Simulation tests for the other modules do not connect them to a real autopilot.
- CI does not run flights, hardware deployments or a complete Gazebo simulation.
- The first GitHub Actions result will only exist after the first push; having a
  workflow file does not mean it has already run on GitHub.
