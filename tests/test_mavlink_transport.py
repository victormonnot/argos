"""Real MAVLink frames over loopback UDP, a local serial PTY and failing I/O.

Only telemetry messages are used. No external vehicle or flight process is needed.
"""
from collections import deque
import math
import os
import select
import socket

import pytest

mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
from argos.backends.mavlink import (
    MavlinkLink, SequenceScope, SendStatus, SerialTransport, TcpTransport, UdpTransport,
)


def heartbeat():
    return mav.MAVLink_heartbeat_message(6, 8, 0, 0, 0, 3)


def frame(seq=0, *, system=1, component=1, message=None, v1=False):
    encoder = mav.MAVLink(None, srcSystem=system, srcComponent=component)
    encoder.seq = seq
    return (message or heartbeat()).pack(encoder, force_mavlink1=v1)


class MemoryTransport:
    def __init__(self, chunks=(), writes=(), datagram=False):
        self.chunks = deque(chunks)
        self.writes = deque(writes)
        self.datagram = datagram
        self.attempted = []
        self.closed = False
        self.reads = 0

    def read(self):
        self.reads += 1
        item = self.chunks.popleft() if self.chunks else b""
        if isinstance(item, Exception):
            raise item
        return item

    def write(self, data):
        self.attempted.append(bytes(data))
        result = self.writes.popleft() if self.writes else len(data)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def link(transport=None, **kwargs):
    return MavlinkLink(transport or MemoryTransport(),
                       sequence_scope=SequenceScope.COMPONENT, **kwargs)


def test_open_and_poll_are_passive_including_vehicle_parameters():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.bind(("127.0.0.1", 0))
        peer.settimeout(.02)
        transport = UdpTransport(local=("127.0.0.1", 0), peer=peer.getsockname())
        with link(transport) as wire:
            assert wire.poll(0.) == ()
            with pytest.raises(socket.timeout):
                peer.recv(65535)
            assert wire.report(0.).traffic.tx == 0
            # In particular, neither PARAM_SET nor an ARM command was sent.


def test_real_udp_roundtrip_counts_encoded_bytes_and_preserves_payload():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.bind(("127.0.0.1", 0))
        peer.settimeout(.5)
        transport = UdpTransport(local=("127.0.0.1", 0), peer=peer.getsockname())
        with link(transport) as wire:
            raw = frame(254)
            peer.sendto(raw, transport.local_address)
            received, = wire.poll(.1)
            assert (received.system, received.component, received.sequence) == (1, 1, 254)
            assert received.received_at == .1 and received.frame == raw
            assert received.type_name == "HEARTBEAT"
            with pytest.raises(TypeError):
                received.fields['type'] = 2
            result = wire.send(heartbeat(), .2)
            outbound, _ = peer.recvfrom(65535)
            decoded = mav.MAVLink(None).parse_buffer(outbound)[0]
            assert decoded.get_type() == "HEARTBEAT"
            assert decoded.get_srcSystem() == 255
            assert result.accepted and result.bytes_written == len(outbound)
            report = wire.report(.2)
            assert report.rx_bytes == len(raw)
            assert report.traffic.rx == report.traffic.tx == 1
            assert report.traffic.tx_bytes_per_s == len(outbound) / 3


def test_udp_does_not_adopt_an_unconfigured_sender():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as foreign:
        peer.bind(("127.0.0.1", 0))
        transport = UdpTransport(local=("127.0.0.1", 0), peer=peer.getsockname())
        with link(transport) as wire:
            foreign.sendto(frame(99, system=99), transport.local_address)
            peer.sendto(frame(0), transport.local_address)
            assert [event.system for event in wire.poll(0.)] == [1]


@pytest.mark.skipif(os.name != 'posix', reason="serial PTY requires POSIX")
def test_real_serial_stream_reassembles_fragments_and_writes_a_complete_frame():
    import pty
    master, slave = pty.openpty()
    try:
        transport = SerialTransport(device=os.ttyname(slave))
        os.set_blocking(master, False)
        with link(transport) as wire:
            raw = frame(5)
            os.write(master, raw[:8])
            assert select.select([slave], [], [], .5)[0]
            assert wire.poll(0.) == ()
            os.write(master, raw[8:])
            assert select.select([slave], [], [], .5)[0]
            events = wire.poll(.1)
            assert len(events) == 1 and events[0].frame == raw
            result = wire.send(heartbeat(), .2)
            assert select.select([master], [], [], .5)[0]
            outbound = os.read(master, 65535)
            assert result.bytes_written == len(outbound)
            assert mav.MAVLink(None).parse_buffer(outbound)[0].get_type() == "HEARTBEAT"
    finally:
        os.close(master)
        os.close(slave)


def test_stream_partial_frame_is_not_counted_or_dated_until_complete():
    raw = frame(2)
    transport = MemoryTransport([raw[:4], b"", raw[4:]])
    wire = link(transport)
    assert wire.poll(.1) == ()
    assert wire.report(.1).traffic.rx == 0
    received, = wire.poll(.2)
    assert received.received_at == .2
    assert wire.report(.2).rx_bytes == len(raw)


def test_udp_partial_frames_cannot_be_spliced_between_datagrams():
    raw = frame(0)
    wire = link(MemoryTransport([raw[:8], raw[8:], raw], datagram=True))
    received = wire.poll(0.)
    assert len(received) == 1 and received[0].frame == raw
    assert wire.report(0.).bad_bytes == len(raw)


def test_crc_failure_and_noise_do_not_enter_the_valid_sequence_stream():
    corrupt = bytearray(frame(1))
    corrupt[-1] ^= 0xff
    wire = link(MemoryTransport([frame(0), b'noise', bytes(corrupt), frame(2)], datagram=True))
    assert [r.sequence for r in wire.poll(0.)] == [0, 2]
    report = wire.report(1.)
    assert report.traffic.sequence.missing == 1
    assert report.bad_bytes == len(corrupt) + 5


def test_all_message_types_are_counted_before_application_filtering():
    attitude = mav.MAVLink_attitude_message(123, .1, .2, .3, 0, 0, 0)
    wire = link(MemoryTransport([frame(0) + frame(1, message=attitude) + frame(2)]))
    messages = wire.poll(0.)
    assert [m.type_name for m in messages] == ['HEARTBEAT', 'ATTITUDE', 'HEARTBEAT']
    assert messages[1].fields['time_boot_ms'] == 123
    assert wire.report(1.).traffic.sequence.loss == 0.


def test_unknown_dialect_frame_cannot_create_a_fictitious_source_or_sequence():
    unsupported = bytearray(frame(1))
    unsupported[7:10] = b'\xff\xff\xff'
    wire = link(MemoryTransport([frame(0) + unsupported + frame(2)]))
    assert [event.sequence for event in wire.poll(0.)] == [0]
    report = wire.report(0.)
    assert report.unsupported_frames == 1 and report.closed
    assert report.traffic.rx == 1
    assert set(report.traffic.by_source) == {(1, 1)}
    assert link().send(mav.MAVLink_unknown(0xffffff, unsupported), 10.).status is SendStatus.INVALID


def test_sequence_scope_handles_shared_and_independent_encoders():
    shared = MemoryTransport([frame(0, component=1), frame(1, component=42), frame(2)])
    wire = MavlinkLink(shared, sequence_scope=SequenceScope.CHANNEL)
    wire.poll(0.)
    assert wire.report(1.).traffic.sequence.loss == 0.
    independent = MemoryTransport([frame(0), frame(90, component=42), frame(1)])
    wire = link(independent)
    wire.poll(0.)
    assert len(wire.report(1.).traffic.by_source) == 2
    assert wire.report(1.).traffic.sequence.loss == 0.


def test_empty_poll_is_bounded_and_never_claims_an_emission():
    transport = MemoryTransport([frame(i) for i in range(20)])
    wire = link(transport)
    assert len(wire.poll(0., max_reads=3)) == 3
    assert transport.reads == 3
    assert wire.report(30.).traffic.ongoing_silence == 30.
    assert wire.report(30.).traffic.tx == 0


@pytest.mark.parametrize('blocked', [0, BlockingIOError()])
def test_zero_write_is_an_attempt_without_a_queued_frame_or_sequence_gap(blocked):
    transport = MemoryTransport(writes=[blocked])
    wire = link(transport)
    assert wire.send(heartbeat(), 1.).status is SendStatus.BLOCKED
    assert wire.report(2.).traffic.ongoing_silence == 2.
    assert wire.poll(2.) == () and len(transport.attempted) == 1
    assert wire.send(heartbeat(), 2.).accepted
    assert transport.attempted[0] == transport.attempted[1]
    assert wire.report(2.).traffic.tx_attempts == 1


def test_partial_write_preserves_known_bytes_closes_and_never_replays_tail():
    transport = MemoryTransport(writes=[5])
    wire = link(transport)
    result = wire.send(heartbeat(), 0.)
    assert result.status is SendStatus.PARTIAL and result.bytes_written == 5
    assert transport.closed
    assert wire.send(heartbeat(), 1.).status is SendStatus.CLOSED
    assert len(transport.attempted) == 1
    report = wire.report(30.)
    assert report.partial_writes == 1 and report.traffic.tx_bytes_per_s == 0.
    assert report.traffic.ongoing_silence == 30.


@pytest.mark.parametrize('failure', [OSError('broken'), None, -1, True, 10000])
def test_unknown_write_count_is_visible_and_never_reported_as_zero(failure):
    wire = link(MemoryTransport(writes=[failure]))
    result = wire.send(heartbeat(), 0.)
    assert result.status is SendStatus.ERROR and result.bytes_written is None
    report = wire.report(0.)
    assert report.unknown_writes == 1 and report.closed
    assert report.traffic.tx_attempts == 0


def test_read_error_closes_without_losing_previously_received_events():
    wire = link(MemoryTransport([frame(0), OSError('disconnected')]))
    assert len(wire.poll(0.)) == 1
    report = wire.report(0.)
    assert report.read_errors == 1 and report.closed and report.traffic.rx == 1


@pytest.mark.parametrize('bad', [math.nan, math.inf, True, None, '10', 10**400])
def test_bad_clock_does_not_touch_io_or_poison_valid_followup(bad):
    transport = MemoryTransport([frame(0)])
    wire = link(transport)
    for operation in [lambda: wire.poll(bad), lambda: wire.send(heartbeat(), bad),
                      lambda: wire.report(bad)]:
        with pytest.raises(ValueError):
            operation()
    assert transport.reads == 0 and not transport.attempted
    assert len(wire.poll(0.)) == 1 and wire.send(heartbeat(), 0.).accepted


def test_invalid_message_does_not_commit_its_future_date():
    transport = MemoryTransport()
    wire = link(transport)
    assert wire.send(object(), 10.).status is SendStatus.INVALID
    invalid = mav.MAVLink_heartbeat_message(300, 8, 0, 0, 0, 3)
    assert wire.send(invalid, 10.).status is SendStatus.INVALID
    assert not transport.attempted
    assert wire.send(heartbeat(), 1.).accepted


def test_accepted_io_and_reads_enforce_chronology_including_empty_polls():
    wire = link()
    assert wire.poll(10.) == ()
    for operation in [lambda: wire.poll(1.), lambda: wire.send(heartbeat(), 1.),
                      lambda: wire.report(1.)]:
        with pytest.raises(ValueError):
            operation()
    assert wire.send(heartbeat(), 10.).accepted


def test_v1_and_v2_are_decoded_and_v1_can_be_explicitly_encoded():
    transport = MemoryTransport([frame(0, v1=True) + frame(1)])
    wire = link(transport)
    assert len(wire.poll(0.)) == 2
    assert wire.send(heartbeat(), 0., force_v1=True).accepted
    assert transport.attempted[-1][0] == 0xfe


def test_importing_transport_does_not_load_its_optional_runtime():
    import subprocess
    import sys
    subprocess.run([sys.executable, '-c',
                    "import argos.backends.mavlink; import sys; "
                    "assert 'pymavlink' not in sys.modules; assert 'serial' not in sys.modules"],
                   check=True)


def test_tcp_fragments_idle_poll_and_eof_preserve_frames_without_transmitting():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        transport = TcpTransport(peer=server.getsockname())
        peer, _ = server.accept()
        with peer, link(transport) as wire:
            assert wire.poll(0.) == ()
            assert not select.select([peer], [], [], .02)[0]
            raw = frame(6)
            peer.sendall(raw[:8])
            assert select.select([transport._socket], [], [], .5)[0]
            assert wire.poll(.1) == ()
            peer.sendall(raw[8:])
            assert select.select([transport._socket], [], [], .5)[0]
            event, = wire.poll(.2)
            assert event.frame == raw and event.received_at == .2
            assert not select.select([peer], [], [], .02)[0]
            # A disconnect after a complete final frame must retain that frame
            # while making the transport failure visible immediately.
            final = frame(7)
            peer.sendall(final)
            peer.shutdown(socket.SHUT_WR)
            assert select.select([transport._socket], [], [], .5)[0]
            events = list(wire.poll(.3))
            if not wire.report(.3).closed:
                assert select.select([transport._socket], [], [], .5)[0]
                events.extend(wire.poll(.4))
            report = wire.report(.4)
            assert [event.frame for event in events] == [final]
            assert report.closed and report.read_errors == 1
            assert report.rx_bytes == len(raw) + len(final)
            assert report.traffic.tx == report.traffic.tx_attempts == 0


@pytest.mark.parametrize('peer', [('localhost', 5760), ('127.0.0.1', 0),
                                ('127.0.0.1', True), ('127.0.0.1', 65536),
                                ('::1', 5760), ['127.0.0.1', 5760]])
def test_tcp_rejects_invalid_peer_before_opening_socket(peer, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid configuration touched the network')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    with pytest.raises(ValueError):
        TcpTransport(peer=peer)
