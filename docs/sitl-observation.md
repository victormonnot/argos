# Observing Gazebo and ArduPilot SITL together

This session shows the onboard camera and vehicle telemetry while on the ground.
The console receives data without sending commands to ArduPilot or the camera.
The guide involves no arming, flight or gimbal movement.

## Reference environment and revisions

Simultaneous reception was verified on **Ubuntu 24.04 x86-64, Python 3.12**,
with Gazebo Harmonic (`gz-sim8` 8.13.0, transport 13.5.0, messages 10.3.2).
Gazebo must be able to initialize its rendering engine, even without a window.
The ARGOS Python package does not provide Gazebo, its graphics driver or SITL.

| Component | Exact reference source |
| --- | --- |
| SITL autopilot | [ArduPilot fork, `8927564c84f4cdb0df6649973a80974f40a3281b`](https://github.com/victormonnot/ardupilot/commit/8927564c84f4cdb0df6649973a80974f40a3281b) |
| Fork base | ArduPilot `740cbb712bcce2f281f30409b44a3901ace8d481`; the added commit only changes the banner to `ArduCopter-ARGOS V4.8.0-dev`. |
| Gazebo plugin and models | [ArduPilot/ardupilot_gazebo, `082a0fe231f6e63bc8d1598f1cba461d9e2ea7f5`](https://github.com/ArduPilot/ardupilot_gazebo/commit/082a0fe231f6e63bc8d1598f1cba461d9e2ea7f5) + [observation adjustments](../examples/gazebo-observation.patch) |
| SITL parameters | [examples/sitl-observation.parm](../examples/sitl-observation.parm) |

The models used for validation differ from upstream in three values:
camera horizontal field of view **1.2 rad**, requested update rate **30 Hz**,
and `lock_step` **0**. The supplied patch reproduces these differences explicitly.
Running without lockstep does not guarantee the same simulation rate on every
machine. ARGOS thresholds remain based on local reception times.
The plugin does not need recompiling for these XML changes. The patch was applied
and compared against the reference models in an isolated directory.

In this SITL revision, `MAV1_*` configures the MAVLink channel associated with SERIAL0.
Older versions use parameters such as `SR0_*`; the supplied file is not claimed
to work with every ArduPilot version. No `ARMING_CHECK` parameter is changed.
The revisions above identify the sources of the verified binary and models,
independently of a fork's current branch.

## Preparing a new installation

Start with an ARGOS checkout and its venv installed as described in the [README](../README.md).
The following commands create the two neighboring repositories; use a fresh
directory if those names already contain personal work.

### Gazebo Harmonic

Follow the [official Ubuntu binary installation guide](https://gazebosim.org/docs/harmonic/install_ubuntu/)
to configure the OSRF package repository and install `gz-harmonic`.
Add the Python bindings and plugin build dependencies:

```sh
sudo apt-get update
sudo apt-get install build-essential cmake git python3-venv \
  libgz-sim8-dev rapidjson-dev libopencv-dev \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl \
  python3-gz-transport13 python3-gz-msgs10
/usr/bin/python3 -c 'import gz.transport13; import gz.msgs10.image_pb2'
```

These dependencies follow the [plugin instructions](https://github.com/ArduPilot/ardupilot_gazebo#installation).
They belong to the Harmonic release family; do not mix library version numbers
with another Gazebo release. ARGOS CI does not perform the full system installation.

### ArduPilot SITL

From the ARGOS repository root, in a terminal used for setup:

```sh
ARGOS_REPO_DIR="$(pwd)"
ARGOS_PROJECT_DIR="$(dirname "$ARGOS_REPO_DIR")"
cd "$ARGOS_PROJECT_DIR"
git clone https://github.com/victormonnot/ardupilot.git
cd ardupilot
git checkout --detach 8927564c84f4cdb0df6649973a80974f40a3281b
git submodule update --init --recursive
python3 -m venv --system-site-packages venv-ardupilot
DO_PYTHON_VENV_ENV=0 Tools/environment_install/install-prereqs-ubuntu.sh -y
. venv-ardupilot/bin/activate
./waf configure --board sitl
./waf copter
```

The [official ArduPilot setup script](https://ardupilot.org/dev/docs/building-setup-linux.html#install-some-required-packages)
installs its dependencies and uses a dedicated ArduPilot venv here. Do not run it
with `sudo`: the script invokes the required system commands itself.
The expected output is `ardupilot/build/sitl/bin/arducopter`; its header
`build/sitl/ap_version.h` should report `8927564c`. A complete build on a fresh
machine was not repeated during preparation of this V1.

### Gazebo plugin and models

In another terminal, from the ARGOS repository root:

```sh
ARGOS_REPO_DIR="$(pwd)"
ARGOS_PROJECT_DIR="$(dirname "$ARGOS_REPO_DIR")"
cd "$ARGOS_PROJECT_DIR"
git clone https://github.com/ArduPilot/ardupilot_gazebo.git
cd ardupilot_gazebo
git checkout --detach 082a0fe231f6e63bc8d1598f1cba461d9e2ea7f5
git apply --check "$ARGOS_REPO_DIR/examples/gazebo-observation.patch"
git apply "$ARGOS_REPO_DIR/examples/gazebo-observation.patch"
export GZ_VERSION=harmonic
cmake -S . -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build -j4
```

The patch only targets the two model files mentioned above; do not apply it
twice. ArduPilot/Gazebo sources and licenses remain in their respective
repositories. ARGOS does not bundle binaries from these projects.

Before starting, ports **9002/UDP, 5760/TCP and 8080/TCP** must be available.
Only one SITL instance should communicate with the world on port 9002.

## Preparing three terminals

In **each of the three terminals**, start from the `argos` repository root,
then run:

```sh
ARGOS_REPO_DIR="$(pwd)"
ARGOS_PROJECT_DIR="$(dirname "$ARGOS_REPO_DIR")"
export GZ_PARTITION=argos-observation
```

Gazebo and the console must use the same partition. It separates their Gazebo
discovery from other sessions, but does not isolate the JSON or TCP ports.

## 1. Start Gazebo

In the first terminal:

```sh
export GZ_SIM_SYSTEM_PLUGIN_PATH="$ARGOS_PROJECT_DIR/ardupilot_gazebo/build${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}"
export GZ_SIM_RESOURCE_PATH="$ARGOS_PROJECT_DIR/ardupilot_gazebo/models:$ARGOS_PROJECT_DIR/ardupilot_gazebo/worlds${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
gz sim -s -r --headless-rendering \
  "$ARGOS_PROJECT_DIR/ardupilot_gazebo/worlds/iris_runway.sdf"
```

Gazebo runs without its own window. The camera sensor in the `iris_with_gimbal`
model provides the image; no video file substitutes for it.

## 2. Start SITL in a fresh directory

In the second terminal:

```sh
ARGOS_RUN_DIR="$(mktemp -d /tmp/argos-sitl-observation.XXXXXX)"
cd "$ARGOS_RUN_DIR"
"$ARGOS_PROJECT_DIR/ardupilot/build/sitl/bin/arducopter" \
  --model JSON \
  --speedup 1 \
  --home=-35.363262,149.165237,584,0 \
  --sim-port-out 9002 \
  --serial0 tcp:5760:wait \
  --defaults "$ARGOS_REPO_DIR/examples/sitl-observation.parm" \
  --sysid 1
```

This fresh directory isolates the persistent parameters and files produced by
SITL. The initial position matches the `iris_runway` world. The JSON model uses
its simulated sensor calibrations. `BATT_MONITOR 4` enables the simulated battery;
the other additions configure the frame and telemetry streams.

The `Waiting for connection` message is expected: `:wait` waits for the TCP client
before continuing. This binary's TCP server listens on **all IPv4 interfaces**,
even though the console connects to localhost. It accepts one client at a time;
do not open MAVProxy or another reader on that port at the same time.

## 3. Start the console

In the third terminal:

```sh
.venv/bin/python -m argos.console \
  --gazebo-topic /world/iris_runway/model/iris_with_gimbal/model/gimbal/link/pitch_link/sensor/camera/image \
  --gazebo-python-path /usr/lib/python3/dist-packages \
  --mavlink-tcp 127.0.0.1:5760 \
  --sequence-scope channel \
  --heartbeat-age 2.5
```

Open **http://127.0.0.1:8080**. The default MAVLink source is system 1, component 1.
The `channel` scope matches this direct connection to an ArduPilot channel.
The console does not request message rates: the parameter file specifies
SYS_STATUS at 2 Hz, ATTITUDE at 10 Hz and positions at 5 Hz.
HEARTBEAT is scheduled separately at 1 Hz.

Image and telemetry states are independent. LOCAL_POSITION_NED may remain absent
while the estimator initializes. Its coordinates are relative to the estimator's
origin; they are not an altitude above ground. Received message rates also depend
on the simulation's actual speed; age thresholds use the local reception time.

## Verification and shutdown

The local test on September 7, 2026 with this configuration received **553 frames
in 12 seconds**, including 13 HEARTBEAT, 20 SYS_STATUS, 101 ATTITUDE and
50 LOCAL_POSITION_NED, with no application-level transmission or unknown-dialect
frame. The 158 bytes outside MAVLink frames at startup were counted as such.
This result verifies a ground observation session, not flight or a connection
to physical hardware.

If nothing arrives, check the messages in all three terminals, the shared Gazebo
partition and port availability. Wait for the TCP connection and then SITL
initialization before concluding that data is missing.

Stop the capture first, then the console, SITL and Gazebo with Ctrl-C in their
terminals. The temporary SITL directory remains available for inspecting its
files. The [operating guide](running.md) covers tmux, SSH access from a laptop
and restarting. These simulation parameters are not a flight configuration;
this validation remains a ground observation session.
