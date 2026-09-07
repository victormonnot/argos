"""Passive acquisition: original times, clean stopping and visible failure."""
from collections import deque
import io
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys

import pytest

mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
from argos.backends.mavlink import (
    CaptureError, MavlinkLink, RecordingError, SequenceScope, capture_session, read_recording,
)

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "examples/mavlink_recording.py"


def frame(sequence=0):
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    encoder.seq = sequence
    return bytes(mav.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3).pack(encoder))


class Time:
    def __init__(self):
        self.now = 100.
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class Input:
    datagram = False

    def __init__(self, *chunks):
        self.chunks = deque(chunks)
        self.reads = 0
        self.closed = False

    def read(self):
        self.reads += 1
        value = self.chunks.popleft() if self.chunks else b""
        if isinstance(value, Exception):
            raise value
        return value

    def write(self, data):
        raise AssertionError("passive capture must never emit")

    def close(self):
        self.closed = True


def link(source):
    return MavlinkLink(source, sequence_scope=SequenceScope.COMPONENT)


def test_capture_preserves_every_reception_and_final_silence_without_sending():
    source, stream, time = Input(frame(1) + frame(2)), io.BytesIO(), Time()
    with link(source) as wire:
        result = capture_session(wire, stream, duration=2., poll_interval=.5,
                                 clock=time.clock, sleep=time.sleep)
        assert result.recorded_events == 2 and result.ended_at == 2.
        assert result.stop_reason == "duration"
        assert result.rx_bytes == len(frame(1) + frame(2)) and result.bad_bytes == 0
        assert not stream.closed and not source.closed
    assert source.closed
    replay = read_recording(io.BytesIO(stream.getvalue()))
    assert replay.ended_at == 2.
    assert [event.received_at for event in replay.events] == [0., 0.]
    assert [event.frame for event in replay.events] == [frame(1), frame(2)]


def test_empty_capture_is_valid_but_does_not_claim_any_messages():
    stream, time = io.BytesIO(), Time()
    with link(Input()) as wire:
        result = capture_session(wire, stream, duration=1., poll_interval=.4,
                                 clock=time.clock, sleep=time.sleep)
    assert result.recorded_events == result.rx_bytes == 0
    recording = read_recording(io.BytesIO(stream.getvalue()))
    assert recording.events == () and recording.ended_at == 1.


def test_stop_during_record_write_finishes_the_entire_polled_batch():
    stopped = False

    class StopAfterFirst(io.BytesIO):
        def write(self, data):
            nonlocal stopped
            if b'"kind":"rx"' in data:
                stopped = True
            return super().write(data)

    stream, time = StopAfterFirst(), Time()
    with link(Input(frame(0) + frame(1))) as wire:
        result = capture_session(wire, stream, duration=10., poll_interval=5.,
                                 stop_requested=lambda: stopped, clock=time.clock, sleep=time.sleep)
    assert result.stop_reason == "requested" and result.recorded_events == 2
    assert time.sleeps == []
    assert len(read_recording(io.BytesIO(stream.getvalue())).events) == 2


def test_link_error_keeps_prefix_but_cannot_finalize_a_successful_journal():
    stream, time = io.BytesIO(), Time()
    with link(Input(frame(), OSError("disconnected"))) as wire:
        with pytest.raises(CaptureError, match="disconnected"):
            capture_session(wire, stream, duration=1., clock=time.clock, sleep=time.sleep)
    assert b'"kind":"rx"' in stream.getvalue()
    assert b'"kind":"end"' not in stream.getvalue()
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(stream.getvalue()))


@pytest.mark.parametrize("bad", [math.nan, math.inf, 99.])
def test_bad_runtime_clock_leaves_an_incomplete_journal(bad):
    dates = iter([100., bad])
    stream = io.BytesIO()
    with link(Input(frame())) as wire:
        with pytest.raises(CaptureError):
            capture_session(wire, stream, duration=1., clock=lambda: next(dates))
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(stream.getvalue()))


@pytest.mark.parametrize("name", ["duration", "poll_interval"])
@pytest.mark.parametrize("bad", [0., -1., True, math.nan, math.inf])
def test_invalid_configuration_does_not_write(name, bad):
    stream, source = io.BytesIO(), Input(frame())
    args = {"duration": 1., "poll_interval": .1, name: bad}
    with link(source) as wire:
        with pytest.raises(ValueError):
            capture_session(wire, stream, **args)
    assert stream.getvalue() == b"" and source.reads == 0


def test_previously_used_link_is_refused_before_header_write():
    stream = io.BytesIO()
    with link(Input(frame())) as wire:
        wire.poll(0.)
        with pytest.raises(ValueError, match="unused"):
            capture_session(wire, stream, duration=1.)
    assert stream.getvalue() == b""


def test_slow_processing_records_actual_end_instead_of_clamping_duration():
    time = Time()

    class SlowInput(Input):
        def read(self):
            time.now += .25
            return super().read()

    stream = io.BytesIO()
    with link(SlowInput(frame())) as wire:
        result = capture_session(wire, stream, duration=.1,
                                 clock=time.clock, sleep=time.sleep)
    assert result.ended_at == .5 and result.stop_reason == "duration"
    assert time.sleeps == []
    assert read_recording(io.BytesIO(stream.getvalue())).ended_at == .5


@pytest.mark.parametrize("interrupt", [False, True], ids=["duration", "ctrl_c"])
@pytest.mark.skipif(os.name != "posix", reason="CLI pipe readiness requires POSIX select")
def test_real_udp_cli_capture_and_replay_without_any_outgoing_message(tmp_path, interrupt):
    path = tmp_path / "capture.jsonl"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.bind(("127.0.0.1", 0))
        peer.settimeout(.05)
        args = [sys.executable, str(CLI), "capture-udp", str(path),
                "--bind", "127.0.0.1:0", "--peer", f"127.0.0.1:{peer.getsockname()[1]}",
                "--duration", "30" if interrupt else ".2", "--sequence-scope", "component"]
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # Read one readiness line with a bounded select, avoiding arbitrary sleeps.
            assert select.select([process.stderr], [], [], 5.)[0]
            ready = process.stderr.readline().strip()
            assert ready.startswith("Listening on 127.0.0.1:"), ready
            port = int(ready.rsplit(":", 1)[1])
            if interrupt:
                # An empty user-stopped capture is also a complete, inspectable session.
                process.send_signal(signal.SIGINT)
            else:
                peer.sendto(frame(0) + frame(1), ("127.0.0.1", port))
            stdout, stderr = process.communicate(timeout=5.)
            assert process.returncode == 0, stderr
            summary = json.loads(stdout)
            assert summary["stop_reason"] == ("requested" if interrupt else "duration")
            recording = read_recording(io.BytesIO(path.read_bytes()))
            assert len(recording.events) == (0 if interrupt else 2)
            assert recording.ended_at == summary["ended_at"]
            with pytest.raises(socket.timeout):
                peer.recv(65535)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


@pytest.mark.skipif(os.name != "posix", reason="serial PTY requires POSIX")
def test_real_serial_cli_capture_preserves_frames_and_never_transmits(tmp_path):
    import pty
    master, slave = pty.openpty()
    path = tmp_path / "serial.jsonl"
    process = None
    try:
        device = os.ttyname(slave)
        process = subprocess.Popen(
            [sys.executable, str(CLI), "capture-serial", str(path), "--device", device,
             "--duration", ".2", "--sequence-scope", "component"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert select.select([process.stderr], [], [], 5.)[0]
        assert process.stderr.readline().strip() == f"Listening on {device}"
        raw = frame(0) + frame(1)
        assert os.write(master, raw) == len(raw)
        stdout, stderr = process.communicate(timeout=5.)
        assert process.returncode == 0, stderr
        summary = json.loads(stdout)
        recording = read_recording(io.BytesIO(path.read_bytes()))
        assert b"".join(event.frame for event in recording.events) == raw
        assert summary["recorded_events"] == 2 and summary["stop_reason"] == "duration"
        # Keep the slave open so EOF cannot be mistaken for an outgoing byte.
        assert not select.select([master], [], [], .05)[0]
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        os.close(master)
        os.close(slave)
