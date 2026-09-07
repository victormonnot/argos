"""Inspect reception ages from encoded telemetry, entirely offline.

    python examples/mavlink_telemetry.py

The byte source simulates delivery delays; pymavlink still encodes and validates
real frames. No network connection, serial device, or flight process is opened.
"""
from collections import deque
import json
import math

from pymavlink.dialects.v20 import ardupilotmega as mav

from argos.backends.mavlink import (
    MavlinkLink, SequenceScope, TelemetryCache, TelemetryLimits,
)


class ReplayTransport:
    datagram = False

    def __init__(self):
        self.chunks = deque()

    def read(self):
        return self.chunks.popleft() if self.chunks else b""

    def write(self, data):
        raise AssertionError("a passive telemetry reader must not write")

    def close(self):
        pass


def summarize(snapshot):
    result = {"inspected_at": snapshot.at}
    for name in ("heartbeat", "attitude", "local_position_ned"):
        view = getattr(snapshot, name)
        message = view.message
        result[name] = {
            "reception_state": view.state.value,
            "rx_age_s": view.rx_age,
            "received_at": message.received_at if message else None,
            "time_boot_ms": message.fields.get("time_boot_ms") if message else None,
            "boot_progress": view.boot_progress.value if view.boot_progress else None,
        }
    result["rejected"] = snapshot.rejected
    result["last_rejection"] = snapshot.last_rejection
    return result


def main():
    transport = ReplayTransport()
    cache = TelemetryCache(system=1, component=1,
                           limits=TelemetryLimits(heartbeat=1., attitude=.2,
                                                  local_position_ned=.4))
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    snapshots = [summarize(cache.snapshot(0.))]
    with MavlinkLink(transport, sequence_scope=SequenceScope.COMPONENT) as link:
        def receive(now, *messages):
            for message in messages:
                transport.chunks.append(message.pack(encoder))
                encoder.seq = (encoder.seq + 1) % 256
            for event in link.poll(now):
                cache.update(event, now)
            snapshots.append(summarize(cache.snapshot(now)))

        receive(.1, mav.MAVLink_attitude_message(200, .1, .2, .3, 0., 0., 0.),
                mav.MAVLink_local_position_ned_message(150, 10., 20., -3., 1., 2., -1.))
        receive(.75, mav.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3))
        receive(.8, mav.MAVLink_attitude_message(300, math.nan, .2, .3, 0., 0., 0.))
        # A newly received packet may repeat an old sender timestamp. Reception
        # age returns to zero; the sender timestamp is still visibly unchanged.
        receive(1., mav.MAVLink_attitude_message(200, .1, .2, .3, 0., 0., 0.))
        report = link.report(1.)
        assert report.traffic.rx == 5 and report.traffic.tx_attempts == 0
        assert snapshots[2]["attitude"]["reception_state"] == "stale"
        assert snapshots[3]["attitude"]["received_at"] == .1
        assert snapshots[4]["attitude"]["boot_progress"] == "repeated"
        print(json.dumps({
            "scope": "offline decoded telemetry; local reception age only",
            "snapshots": snapshots,
            "decoded_frames": report.traffic.rx,
            "tx_attempts": report.traffic.tx_attempts,
            "bad_bytes": report.bad_bytes,
        }, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
