# SITL replay example

This directory contains an **extract of telemetry actually received from
ArduPilot SITL on the ground**, prepared from a validation capture made on
September 7, 2026. It contains no images or video. The 58 selected frames are
HEARTBEAT, SYS_STATUS, ATTITUDE and LOCAL_POSITION_NED from system 1, component 1.

The MAVLink bytes, order and local reception timestamps of the retained frames
are unchanged. The source recording's start and end bounds are preserved.
Other message types are omitted, and the extract has its own checksum.
Sequence gaps in this extract therefore cannot be used to estimate packet loss
on the original link.

The [manifest](manifest.json) describes the selection and the SHA-256 hashes of
both the source capture and the extract. The source was an ARGOS v1 journal
without context: its exact capture time in UTC, simulator/firmware versions and
original settings were not recorded. They are not reconstructed from the current
configuration. Attribution to a SITL ground trial comes from the developer's
retained validation context, not from proof of origin supplied by the protocol.
The file is an analysis example, not a performance benchmark.

From the repository root, after installing the `console,mavlink` extras:

```sh
python examples/verify_demo.py
python -m argos.console --recordings-dir examples/demo --port 8081
```

Open **http://127.0.0.1:8081 → Sessions**. The journal shows approximately 3.14
seconds of receptions; measurements, messages and charts can be explored without
starting Gazebo or SITL. Historical values are evaluated at the replay cursor
using their recorded reception times. Observation has no live source. This directory is intended for replay;
for a personal capture, restart the console with its usual recording directory.
