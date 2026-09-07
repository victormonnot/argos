"""Reproduce a telemetry exchange locally; no autopilot or serial hardware.

    python examples/mavlink_loopback.py
"""
from dataclasses import asdict
import json
import socket

from pymavlink.dialects.v20 import ardupilotmega as mav

from argos.backends.mavlink import MavlinkLink, SequenceScope, UdpTransport


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.bind(("127.0.0.1", 0))
        peer.settimeout(.5)
        transport = UdpTransport(local=("127.0.0.1", 0), peer=peer.getsockname())
        with MavlinkLink(transport, sequence_scope=SequenceScope.COMPONENT) as link:
            encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
            message = mav.MAVLink_heartbeat_message(6, 8, 0, 0, 0, 3)
            incoming = message.pack(encoder)
            peer.sendto(incoming, transport.local_address)
            received = link.poll(.1)
            if len(received) != 1:
                raise RuntimeError("local heartbeat was not received")
            sent = link.send(mav.MAVLink_heartbeat_message(6, 8, 0, 0, 0, 3), .2)
            outgoing, _ = peer.recvfrom(65535)
            decoded = mav.MAVLink(None).parse_buffer(outgoing)[0]
            report = link.report(.2)
            assert sent.accepted and sent.bytes_written == len(outgoing)
            assert decoded.get_type() == received[0].type_name == "HEARTBEAT"
            assert report.traffic.rx == report.traffic.tx == 1
            assert report.rx_bytes == len(incoming) and report.bad_bytes == 0
            traffic = asdict(report.traffic)
            traffic['by_source'] = [
                {"system": key[0], "component": key[1], **asdict(counts)}
                for key, counts in report.traffic.by_source.items()
            ]
            print(json.dumps({
                "scope": "loopback telemetry; no vehicle parameters or flight commands",
                "received": received[0].type_name,
                "decoded_by_peer": decoded.get_type(),
                "tx_status": sent.status.value,
                "tx_bytes_accepted_locally": sent.bytes_written,
                "rx_bytes": report.rx_bytes,
                "traffic": traffic,
                "loss": report.traffic.sequence.loss,
            }, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
