"""Finite vision bridge: real source deadlines, bounded serial exchange, stop paths."""
from collections import deque
from dataclasses import replace
import subprocess
import sys

import pytest

from argos.backends import edgetx_vision_bench as bridge
from argos.backends.vision_bench_source import PreviewValue


class Clock:
    def __init__(self):
        self.now = 100.

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Radio:
    """Independent expiry clock, with controllable I/O timing and corruptions."""
    def __init__(self, clock, greeting=bridge.HELLO + b"\n"):
        self.clock = clock
        self.chunks = deque([greeting])
        self.writes = []
        self.times = []
        self.expiry = None
        self.sequence = 0
        self.session = None
        self.transform = lambda data: data
        self.write_delay = 0.
        self.partial = False
        self.closed = False

    def reset_input_buffer(self):
        pass  # queued greeting models a new hello after the reset

    def read(self, maximum):
        assert maximum == 256
        if self.chunks:
            return self.chunks.popleft()
        if self.expiry is not None and self.clock.now >= self.expiry:
            self.expiry = None
            return f"ARGOS_VISION_IDLE {self.session} {self.sequence}\n".encode()
        return b""

    def write(self, data):
        self.writes.append(data)
        self.times.append(self.clock.now)
        self.clock.sleep(self.write_delay)
        if self.partial:
            return len(data) - 1
        parts = data.decode().split()
        if parts[0] == "ARGOS_VISION_BEGIN":
            _, self.session = parts
            reply = f"ARGOS_VISION_READY {self.session}\n"
        else:
            assert parts[0] == "ARGOS_VISION_SET"
            _, session, seq, value, ttl = parts
            assert session == self.session
            self.sequence = int(seq)
            self.expiry = self.clock.now + int(ttl) / 100
            reply = f"ARGOS_VISION_ACK {session} {seq} {value} {ttl}\n"
        self.chunks.append(self.transform(reply.encode()))
        return len(data)

    def close(self):
        self.closed = True


def make(greeting=bridge.HELLO + b"\n"):
    clock = Clock()
    radio = Radio(clock, greeting)
    bench = bridge.VisionBench(radio, clock=clock.clock, sleep=clock.sleep)
    bench.session = "1234abcd"
    return bench, radio, clock


def proposal(clock, value=70, lifetime=.4):
    return PreviewValue(value, clock.now + lifetime, 1, 7, 1, "run", "camera", 1., 100.)


class Source:
    def __init__(self, clock):
        self.clock = clock
        self.reads = 0
        self.delays = {}
        self.fail_at = None
        self.values = []
        self.closed = False

    def read(self):
        self.reads += 1
        self.clock.sleep(self.delays.get(self.reads, 0.))
        if self.reads == self.fail_at:
            raise bridge.PreviewError("selection cleared")
        value = replace(proposal(self.clock), frame_sequence=self.reads)
        self.values.append(value)
        return value

    def close(self):
        self.closed = True


def test_normal_session_refreshes_after_handshake_is_finite_and_expires(capsys):
    bench, radio, clock = make()
    source = Source(clock)
    bridge.run_bridge(source, bench, 1)
    sets = [line.decode().split() for line in radio.writes[1:]]
    assert 10 <= len(sets) <= 11
    assert source.reads == len(sets) + 1
    assert all(parts[0] == "ARGOS_VISION_SET" and parts[4] == "20" for parts in sets)
    assert all(b - a >= .1 - 1e-9 for a, b in zip(radio.times[1:], radio.times[2:]))
    assert radio.write_timeout == .02
    assert bench.finished
    assert clock.now >= radio.times[-1] + .2
    assert "Lua reports expiry" in capsys.readouterr().out
    with pytest.raises(bridge.ProbeError):
        bench.send(proposal(clock))


@pytest.mark.parametrize("greeting", [b"", b"CLI>\n", b"ARGOS_USB_MIX_BENCH_V1\n",
                                     b"ARGOS_USB_DISPLAY_V1\n", bridge.HELLO])
def test_wrong_peer_never_gets_any_bytes(greeting):
    bench, radio, clock = make(greeting)
    with pytest.raises(bridge.ProbeError):
        bridge.run_bridge(Source(clock), bench, 1)
    assert not radio.writes and bench.failed


@pytest.mark.parametrize("fail_at,writes", [(1, 0), (2, 1), (4, 3)])
def test_source_failure_stops_without_cleanup_or_restart(fail_at, writes):
    bench, radio, clock = make()
    source = Source(clock)
    source.fail_at = fail_at
    with pytest.raises(bridge.PreviewError):
        bridge.run_bridge(source, bench, 1)
    assert len(radio.writes) == writes and bench.failed
    with pytest.raises(bridge.ProbeError):
        bench.send(proposal(clock))
    with pytest.raises(bridge.ProbeError):
        bench.connect()
    assert len(radio.writes) == writes


@pytest.mark.parametrize("value", [-129, 129, True, 1.2, float("nan")])
def test_invalid_value_latches_session_before_set(value):
    bench, radio, clock = make()
    bench.connect()
    with pytest.raises(bridge.ProbeError):
        bench.send(proposal(clock, value=value))
    assert len(radio.writes) == 1 and bench.failed


@pytest.mark.parametrize("deadline", [float("inf"), float("nan"), True, None, 100., 100.039])
def test_invalid_or_close_deadline_sends_nothing(deadline):
    bench, radio, clock = make()
    bench.connect()
    with pytest.raises(bridge.ProbeError):
        bench.send(replace(proposal(clock), deadline=deadline))
    assert len(radio.writes) == 1 and bench.failed


def test_short_remaining_image_lifetime_becomes_short_radio_ttl():
    bench, radio, clock = make()
    bench.connect()
    seq, ttl = bench.send(proposal(clock, lifetime=.155))
    assert seq == 1 and ttl == 12
    assert radio.times[-1] + ttl * .01 + bridge.WRITE_TIMEOUT + bridge.TICK < 100.155


def test_same_frame_deadline_dwindles_and_cannot_be_extended_by_host():
    bench, radio, clock = make()
    bench.connect()
    selected = proposal(clock, lifetime=.4)
    lifetimes = []
    for _ in range(4):
        bench.pace()
        _, ttl = bench.send(selected)
        lifetimes.append(ttl)
    assert lifetimes[:2] == [20, 20]
    assert lifetimes[2] < 20 and lifetimes[3] < lifetimes[2]
    with pytest.raises(bridge.ProbeError, match="between commands"):
        bench.pace()  # The radio expires before the next scheduled send.
    assert len(radio.writes) == 5 and bench.failed


@pytest.mark.parametrize("response", [
    b"", b"ARGOS_VISION_ACK deadbeef 1 70 20\n",
    b"ARGOS_VISION_ACK 1234abcd 2 70 20\n",
    b"ARGOS_VISION_ACK 1234abcd 1 71 20\n",
    b"ARGOS_VISION_ACK 1234abcd 1 70 19\n",
    b"ARGOS_VISION_IDLE 1234abcd 1\n",
    b"ARGOS_VISION_ACK 1234abcd 1 70 20\nARGOS_VISION_IDLE 1234abcd 1\n",
    b"ARGOS_VISION_ACK 1234abcd 1 70 20\nARGOS_VISION_ACK 1234abcd 1 70 20\n",
    b"x" * 65 + b"\n",
])
def test_bad_ack_including_trailing_batch_is_terminal(response):
    bench, radio, clock = make()
    bench.connect()
    radio.transform = lambda data: response
    with pytest.raises(bridge.ProbeError):
        bench.send(proposal(clock))
    assert len(radio.writes) == 2 and bench.failed
    with pytest.raises(bridge.ProbeError):
        bench.send(proposal(clock))


def test_extra_greetings_are_allowed_but_not_unknown_messages():
    bench, radio, clock = make()
    bench.connect()
    radio.transform = lambda data: bridge.HELLO + b"\n" + data + bridge.HELLO + b"\n"
    bench.send(proposal(clock))
    radio.chunks.append(b"ARGOS_VISION_READY 1234abcd\n")
    with pytest.raises(bridge.ProbeError, match="between commands"):
        bench.pace()
    assert len(radio.writes) == 2


@pytest.mark.parametrize("delay", [.151, .251])
def test_slow_source_after_command_does_not_revive_expired_session(delay):
    bench, radio, clock = make()
    source = Source(clock)
    source.delays[3] = delay
    with pytest.raises(bridge.ProbeError):
        bridge.run_bridge(source, bench, 1)
    assert len(radio.writes) == 2 and bench.failed


@pytest.mark.parametrize("delay,partial", [(.021, False), (0., True)])
def test_late_or_partial_write_prevents_further_commands(delay, partial):
    bench, radio, clock = make()
    bench.connect()
    radio.write_delay, radio.partial = delay, partial
    with pytest.raises(bridge.ProbeError):
        bench.send(proposal(clock))
    assert len(radio.writes) == 2 and bench.failed


def test_deadline_is_checked_after_serial_read_time():
    bench, radio, clock = make()
    bench.connect()
    chosen = proposal(clock, lifetime=.1)
    read = radio.read
    def delayed_read(maximum):
        clock.sleep(.07)
        return read(maximum)
    radio.read = delayed_read
    with pytest.raises(bridge.ProbeError, match="deadline"):
        bench.send(chosen)
    assert len(radio.writes) == 1


def test_scheduler_stall_at_write_latches_failure_even_with_a_valid_ack():
    bench, radio, clock = make()
    bench.connect()
    # Model a process stall after the final clock observation. Such scheduling
    # cannot be made atomic with OS I/O; it must still fail the session rather
    # than accept the late ACK or send another value.
    radio.write_delay = .3
    with pytest.raises(bridge.ProbeError, match="timing budget"):
        bench.send(proposal(clock, lifetime=.15))
    assert bench.failed and len(radio.writes) == 2
    with pytest.raises(bridge.ProbeError):
        bench.send(proposal(clock))


def test_regressing_host_clock_is_terminal():
    bench, radio, clock = make()
    bench.connect()
    clock.now -= 1
    with pytest.raises(bridge.ProbeError, match="clock"):
        bench.send(proposal(clock))
    assert len(radio.writes) == 1 and bench.failed


def test_rate_and_command_count_are_bounded():
    bench, radio, clock = make()
    bench.connect()
    bench.send(proposal(clock))
    with pytest.raises(bridge.ProbeError, match="rate"):
        bench.send(proposal(clock))
    assert len(radio.writes) == 2
    bench, radio, clock = make()
    bench.connect()
    bench.next_sequence = 301
    with pytest.raises(bridge.ProbeError, match="300"):
        bench.send(proposal(clock))
    assert len(radio.writes) == 1


def test_max_duration_stops_before_radio_session_ceiling():
    bench, radio, clock = make()
    bridge.run_bridge(Source(clock), bench, 30)
    assert len(radio.writes) <= 301
    assert radio.times[-1] < bench.session_deadline
    assert bench.finished


@pytest.mark.parametrize("argv", [["--duration", "0"], ["--duration", "31"],
                                   ["--console-port", "0"], ["--console-port", "65536"]])
def test_cli_rejects_invalid_options_before_open(monkeypatch, argv):
    monkeypatch.setattr(bridge, "open_port", lambda port: pytest.fail("opened port"))
    with pytest.raises(SystemExit) as exc:
        bridge.main(["--port", "pocket", *argv])
    assert exc.value.code == 2


@pytest.mark.parametrize("failure,status", [(bridge.PreviewError("clear"), 1),
                                           (KeyboardInterrupt(), 130), (None, 0)])
def test_cli_always_closes_both_connections(monkeypatch, failure, status):
    _, radio, clock = make()
    source = Source(clock)
    monkeypatch.setattr(bridge, "PreviewSource", lambda **kw: source)
    monkeypatch.setattr(bridge, "open_port", lambda port: radio)
    def run(*args):
        if failure is not None:
            raise failure
    monkeypatch.setattr(bridge, "run_bridge", run)
    assert bridge.main(["--port", "pocket"]) == status
    assert source.closed and radio.closed and radio.writes == []


def test_standalone_help_needs_no_installed_argos():
    result = subprocess.run([sys.executable, "argos/backends/edgetx_vision_bench.py", "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0 and "--console-port" in result.stdout
