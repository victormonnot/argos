"""Native loopback UDP and independent-process checks for the SITL pilot radio."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import time

import pytest

from argos.backends.sitl_radio import DEFAULT_CHANNELS, SITLRadioTransport


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def receiver():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.bind(("127.0.0.1", 0))
        peer.settimeout(.5)
        yield peer


def test_open_is_passive_and_native_packets_carry_independent_pilot_channels():
    with receiver() as peer, SITLRadioTransport(peer=peer.getsockname()) as radio:
        peer.settimeout(.03)
        with pytest.raises(socket.timeout):
            peer.recv(1024)
        # Stick axes, throttle, mode, arm and override-enable switch remain
        # independent channels in the native receiver packet, not MAVLink axes.
        channels = (1400, 1600, 1300, 1700, 1100, 1900, 1100, 1500)
        assert radio.send(channels) == 16
        packet, address = peer.recvfrom(1024)
        assert address[0] == "127.0.0.1"
        assert struct.unpack("=8H", packet) == channels
        extended = channels + (1100, 1900, 1500, 1500, 1500, 1500, 1500, 1500)
        assert radio.send(extended) == 32
        assert struct.unpack("=16H", peer.recv(1024)) == extended


@pytest.mark.parametrize("peer", [
    ("192.168.1.2", 5501), ("0.0.0.0", 5501), ("localhost", 5501),
    ("::1", 5501), ("127.0.0.1", 0), ("127.0.0.1", 65536),
    ("127.0.0.1", True), ("127.0.0.1", 5501.0), (2130706433, 5501),
    ["127.0.0.1", 5501], None,
])
def test_transport_refuses_nonliteral_nonloopback_or_invalid_peers(peer):
    with pytest.raises(ValueError):
        SITLRadioTransport(peer=peer)


@pytest.mark.parametrize("channels", [
    [1500] * 7, [1500] * 9, [1500] * 17, "1500" * 8, None,
    [True] + [1500] * 7, [1500.] * 8, [1099] + [1500] * 7,
    [1901] + [1500] * 7, [0] + [1500] * 7, [65535] + [1500] * 7,
])
def test_invalid_values_cannot_send_ignore_sentinels_or_outside_profile_values(channels):
    with receiver() as peer, SITLRadioTransport(peer=peer.getsockname()) as radio:
        with pytest.raises(ValueError):
            radio.send(channels)
        peer.settimeout(.01)
        with pytest.raises(socket.timeout):
            peer.recv(1024)


def test_closed_transport_never_reopens_or_sends():
    with receiver() as peer:
        radio = SITLRadioTransport(peer=peer.getsockname())
        radio.close()
        radio.close()
        with pytest.raises(RuntimeError, match="closed"):
            radio.send(DEFAULT_CHANNELS)


class RadioProcess:
    def __init__(self, peer):
        host, port = peer.getsockname()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "argos.backends.sitl_radio", "--rc-peer", f"{host}:{port}"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.pending = bytearray()

    def event(self, timeout=3.):
        until = time.monotonic() + timeout
        while b"\n" not in self.pending:
            remaining = until - time.monotonic()
            assert remaining > 0, "virtual radio did not acknowledge its command"
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            assert ready, "virtual radio did not produce its event before timeout"
            data = os.read(self.process.stdout.fileno(), 4096)
            assert data, "virtual radio exited without the expected event"
            self.pending.extend(data)
        raw, _, rest = self.pending.partition(b"\n")
        self.pending = bytearray(rest)
        return json.loads(raw)

    def command(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        self.process.stdin.flush()
        return self.event()

    def close(self):
        if self.process.poll() is None:
            if not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=3.)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3.)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()


@pytest.fixture
def radio_process():
    with receiver() as peer:
        radio = RadioProcess(peer)
        try:
            ready = radio.event()
            assert ready["event"] == "ready"
            assert ready["channels"] == list(DEFAULT_CHANNELS)
            yield radio, peer
        finally:
            radio.close()


def drain(peer):
    peer.setblocking(False)
    try:
        while True:
            peer.recv(1024)
    except BlockingIOError:
        pass
    finally:
        peer.settimeout(.5)


def test_separate_process_keeps_current_radio_inputs_without_new_ipc(radio_process):
    radio, peer = radio_process
    channels = [1500, 1450, 1375, 1560, 1100, 1900, 1900, 1100]
    ack = radio.command({"channels": channels})
    assert ack["event"] == "ack" and ack["channels"] == channels
    drain(peer)
    for _ in range(4):
        assert struct.unpack("=8H", peer.recv(1024)) == tuple(channels)
    assert radio.process.poll() is None


def test_pause_stops_udp_only_and_resume_preserves_current_pilot_values(radio_process):
    radio, peer = radio_process
    ack = radio.command({"paused": True})
    assert ack["event"] == "ack" and ack["paused"] is True
    drain(peer)
    peer.settimeout(.08)
    with pytest.raises(socket.timeout):
        peer.recv(1024)
    channels = [1500, 1500, 1400, 1500, 1900, 1900, 1100, 1100]
    assert radio.command({"channels": channels})["paused"] is True
    assert radio.command({"paused": False})["paused"] is False
    assert struct.unpack("=8H", peer.recv(1024)) == tuple(channels)


@pytest.mark.parametrize("command", [
    {"channels": [1900] * 7}, {"channels": [1500] * 8, "paused": True},
    {"paused": 1}, {"stop": False}, {"arm": True}, [],
])
def test_invalid_ipc_has_no_partial_effect_on_live_pilot_values(radio_process, command):
    radio, peer = radio_process
    assert radio.command(command)["event"] == "error"
    drain(peer)
    assert struct.unpack("=8H", peer.recv(1024)) == DEFAULT_CHANNELS
    assert radio.process.poll() is None


def test_explicit_stop_acknowledges_and_exits_cleanly(radio_process):
    radio, _ = radio_process
    assert radio.command({"stop": True})["event"] == "ack"
    assert radio.event()["event"] == "stopped"
    assert radio.process.wait(timeout=3.) == 0


def test_stdin_eof_stops_process_without_inventing_a_receiver_loss(radio_process):
    radio, _ = radio_process
    radio.process.stdin.close()
    assert radio.event()["event"] == "stopped"
    assert radio.process.wait(timeout=3.) == 0


def test_oversized_unterminated_ipc_is_bounded_and_ends_the_sender(radio_process):
    radio, _ = radio_process
    radio.process.stdin.write(b"x" * 4097)
    radio.process.stdin.flush()
    assert radio.event() == {"event": "error", "error": "Command exceeds 4096 bytes"}
    assert radio.process.wait(timeout=3.) == 2


def test_cli_rejects_physical_peer_before_starting_sender():
    result = subprocess.run(
        [sys.executable, "-m", "argos.backends.sitl_radio", "--rc-peer", "192.168.1.5:5501"],
        cwd=ROOT, capture_output=True, timeout=3.,
    )
    assert result.returncode == 2 and result.stdout == b""
