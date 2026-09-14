# Virtual-radio and assistance bench

This runnable example checks how ArduCopter SITL shares control between an
independent virtual pilot and selective ARGOS assistance. It runs an automated
Gazebo flight, records observations and produces an acceptance report. No radio
or drone hardware is needed.

The virtual pilot sends native SITL receiver input from its own process. A
separate assistance process sends MAVLink overrides for pitch and yaw. The
ordinary ARGOS console receives the camera and telemetry passively; the scenario
does not claim browser flight ownership or add radio controls to the interface.

## Run the scenario

Prepare the existing [Gazebo/SITL installation](sitl-observation.md), including
its observation-model adjustments. Follow [model and person-scene setup](vision.md#prepare-the-optional-model-and-scene)
to install the optional inference runtime and download the verified local assets.
Prepare the S model as described in that guide. From the ARGOS repository root,
the reference launch uses S with four inference threads:

```sh
.venv/bin/python examples/run_radio_bench.py \
  --ardupilot-dir ../ardupilot \
  --gazebo-dir ../ardupilot_gazebo \
  --vision-model "$HOME/.cache/argos/models/yolox_s.onnx" \
  --vision-variant s \
  --vision-threads 4 \
  --output-dir /tmp/argos-radio-bench-example
```

Use the model path printed by setup if your cache location differs.
`--vision-model` is required. Tiny remains an available option with its matching
model path and `--vision-variant tiny`; the launcher defaults are Tiny and two
threads. The complete bench has not yet been validated with Tiny.
`--scene inspection` selects the compact yard instead of the default person
scene; `--gui` opens Gazebo's window.

The output directory must be new or empty. Choose a new name for another run,
or omit `--output-dir` to create a fresh `argos-radio-bench-*` temporary directory.
The launcher prints its location and the passive console URL,
**http://127.0.0.1:8083**. The scenario proceeds automatically; watching the
console is optional. It exits with code 0 only when its report passes, or code 1
on a reported failure or interruption. **Ctrl-C** stops this run.

| Option | Default | Connection |
| --- | ---: | --- |
| `--port` | 8083 | Passive console HTTP |
| `--mavlink-port` | 5960 | SITL serial0, passive console |
| `--assistance-port` | 5962 | SITL serial1, assistance process |
| `--monitor-port` | 5964 | SITL serial2, scenario observations |
| `--physics-port` | 9024 | Gazebo physics UDP |
| `--rc-port` | 5521 | Native SITL receiver UDP |

All six ports must differ and be available. ARGOS client destinations and its
HTTP server use loopback. The existing SITL binary's native listeners can bind
all IPv4 interfaces, as described in the [SITL guide](sitl-observation.md).
The launcher creates its own Gazebo partition, private fixed-camera model copy,
parameter storage and recordings directory. It stops its own children after
completion or failure and preserves existing services, models and recordings.
It runs the existing SITL binary; it does not rebuild or flash firmware.

## What it checks

The [bench profile](../examples/sitl-radio-bench.parm) is exclusively for this
simulation. It preserves GPS-free flight and arming checks, enables native radio
mode/arm/assistance switches and gives selective overrides a 0.5-second timeout.

| Channel | Owner in this example |
| --- | --- |
| Roll and throttle | Virtual pilot throughout |
| Pitch and yaw | Virtual pilot, temporarily overridden by explicitly engaged assistance |
| Flight mode, arm/disarm and other AUX inputs | Virtual pilot throughout |
| Assistance permission switch | Virtual pilot; turning it off rejects overrides |

The automated sequence is:

1. **Disarmed switch test.** A bounded two-second probe sends fixed, non-neutral
   pitch/yaw values. The pilot turns assistance permission off while the probe
   keeps sending. Received controller channels must show pilot values despite
   the conflicting transmissions. This is a deliberate arbitration stimulus,
   not image guidance.
2. **Disarmed process-loss test.** The scenario kills an actively transmitting
   assistance process with `SIGKILL`, without a cooperative release. It checks
   recovery of pilot channels while the virtual radio and observer remain alive.
   A restarted assistant must remain disengaged.
3. **Rendered-camera flight.** The scripted pilot arms normally and takes off in
   Stabilize. The assistant explicitly selects an admissible person detection
   from the actual rendered camera and uses the existing pilot-throttle framing
   law. Three throttle changes must reach the controller while image assistance
   continues and keeps roll/throttle released. A non-neutral image correction
   must also appear in the controller's received channels. Turning the pilot switch off
   cancels framing; restoring it must not silently reengage.
4. **Receiver-loss fallback.** A separate simulated receiver failure must select
   Land. The scenario checks landing and disarming before finishing.

Native SITL UDP input retains its last received values and continues generating
receiver frames when UDP sending stops. Stopping that sender alone is therefore
not a radio-loss test. The final stage explicitly injects `SIM_RC_FAIL=1` in
this isolated simulator, then clears the injection after landing.

## Inspect the evidence

| Run output | Contents |
| --- | --- |
| `report.json` | Pass/failure status, completed checks, evidence and finalized capture counters/identifiers |
| `bench-events.jsonl` | Scenario stages, pilot requests, worker status, sent-override metadata and received telemetry |
| `manifest.json` | Process arguments and identity, ports, model/profile hashes, existing binary hash and version header, adjacent source HEAD |
| `recordings/` | Native console MAVLink/visual recordings once capture has started |
| `*.log`, `*.stderr.log` | Simulator, console and worker diagnostics |

The source HEAD and build version header are recorded separately; an adjacent
checkout does not prove that the binary was built from its current HEAD.
Reported reception delays include host scheduling and telemetry delivery; they
are not measurements of a physical radio link or exact firmware transition time.

After the launcher exits, reopen the native recording without a simulator:

```sh
.venv/bin/python -m argos.console \
  --recordings-dir /tmp/argos-radio-bench-example/recordings \
  --port 8083
```

Open **Sessions → Flight replay** to inspect images and detections, and
**Measurements** for received vehicle mode, arming and other telemetry. The
flight replay's control pane records the passive console's disabled control;
it does not know the external assistant's state. **Framing report does not
describe its assistance intervals**. Use
`bench-events.jsonl` and `report.json` for those events and checks; no browser
lease or browser-control history is fabricated.

This bench evaluates the specified simulation and records each run's actual
outcome. It does not implement an ELRS link, a Betaflight adapter or physical
flight control, and passing it does not establish outdoor tracking performance,
radio timing, horizontal position hold or readiness for a real flight.
