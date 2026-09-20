"""Fixed mixer-bench protocol, expiry observation and terminal failure behavior."""
from collections import deque
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from argos.backends import edgetx_mix_probe as probe


SESSION = "a1b2c3d4"


class Clock:
    def __init__(self):
        self.now = 0.

    def clock(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class Radio:
    """A protocol peer with independently scheduled 300 ms expiry reports."""

    def __init__(self, clock, chunks=None):
        self.clock = clock
        self.chunks = deque([probe.HELLO + b"\n"] if chunks is None else chunks)
        self.writes = []
        self.write_times = []
        self.closed = False
        self.expiry = None
        self.session = None
        self.sequence = None
        self.partial_on = None
        self.error_on = None
        self.transform = lambda line: line

    def reset_input_buffer(self):
        # Queued chunks represent bytes arriving after the initial buffer clear.
        pass

    def read(self, maximum):
        assert maximum == 256
        if self.chunks:
            return self.chunks.popleft()
        if self.expiry is not None and self.clock.now >= self.expiry:
            self.expiry = None
            return f"ARGOS_MIX_IDLE {self.session} {self.sequence}\n".encode()
        return b""

    def write(self, data):
        self.writes.append(data)
        self.write_times.append(self.clock.now)
        if self.error_on and len(self.writes) == self.error_on[0]:
            raise self.error_on[1]
        if len(self.writes) == self.partial_on:
            return len(data) - 1
        fields = data.decode().split()
        if fields[0] == "ARGOS_MIX_BEGIN":
            _, self.session = fields
            reply = f"ARGOS_MIX_READY {self.session}\n".encode()
        else:
            assert fields[0] == "ARGOS_MIX_SET"
            _, session, sequence, value = fields
            assert session == self.session
            self.sequence = int(sequence)
            self.expiry = self.clock.now + .3
            reply = f"ARGOS_MIX_ACK {session} {sequence} {value}\n".encode()
        self.chunks.append(self.transform(reply))
        return len(data)

    def close(self):
        self.closed = True


def make(chunks=None):
    clock = Clock()
    port = Radio(clock, chunks)
    mix = probe.MixProbe(port, clock=clock.clock, sleep=clock.sleep)
    mix.session = SESSION
    return mix, port, clock


@pytest.mark.parametrize("noise", [b"", b"CLI> ", b"ARGOS_USB_DISPLAY_V1\n",
                                  b"xARGOS_USB_MIX_BENCH_V1\n",
                                  b"ARGOS_USB_MIX_BENCH_V1 without newline"])
def test_no_writes_to_wrong_or_silent_peer(noise):
    mix, port, clock = make([noise])
    with pytest.raises(probe.ProbeError, match="timeout"):
        mix.connect()
    assert clock.now == pytest.approx(5.)
    with pytest.raises(probe.ProbeError):
        mix.set_value(256)
    assert port.writes == []


def test_session_is_random_lowercase_eight_hex_characters():
    first, _, _ = make()
    fresh = probe.MixProbe(first.port)
    assert re.fullmatch("[0-9a-f]{8}", fresh.session)


def test_complete_pattern_is_fixed_bounded_paced_and_observes_two_expiries(capsys):
    mix, port, clock = make([b"boot\nARGOS_USB_", b"MIX_BENCH_V1\r\n"])
    mix.connect()
    assert port.writes == [f"ARGOS_MIX_BEGIN {SESSION}\n".encode()]
    probe.run_pattern(mix)
    values = [0] * 10 + [256] * 20 + [-256] * 20 + [0] * 10
    assert port.writes[1:] == [
        f"ARGOS_MIX_SET {SESSION} {number} {value}\n".encode()
        for number, value in enumerate(values, 1)
    ]
    times = port.write_times[1:]
    assert all(b - a >= .1 - 1e-9 for a, b in zip(times, times[1:]))
    # Expiry plus the visible 500 ms neutral pause precedes the negative phase.
    assert times[30] - times[29] >= .8 - 1e-9
    assert clock.now - times[-1] >= .3 - 1e-9
    assert mix.idle_sequence == 60
    output = capsys.readouterr().out
    assert "IDLE 1/2 after ACK 30" in output
    assert "IDLE 2/2 after ACK 60" in output
    assert "ACK 60/60" in output


def test_stale_input_is_cleared_before_waiting_for_greeting():
    mix, port, _ = make()
    port.reset_input_buffer = port.chunks.clear
    with pytest.raises(probe.ProbeError, match="timeout"):
        mix.connect()
    assert port.writes == []


def test_oversized_line_cannot_smuggle_greeting():
    mix, port, _ = make([b"x" * 64 + probe.HELLO + b"\n"])
    with pytest.raises(probe.ProbeError, match="oversized"):
        mix.connect()
    assert port.writes == []


@pytest.mark.parametrize("reply", [b"", b"ARGOS_MIX_READY deadbeef\n",
                                   b"ARGOS_MIX_ACK a1b2c3d4 1 0\n"])
def test_begin_requires_matching_ready_before_any_set(reply):
    mix, port, _ = make()
    port.transform = lambda _: reply
    with pytest.raises(probe.ProbeError):
        mix.connect()
    with pytest.raises(probe.ProbeError):
        mix.set_value(0)
    assert port.writes == [f"ARGOS_MIX_BEGIN {SESSION}\n".encode()]


@pytest.mark.parametrize("reply", [
    b"", b"ARGOS_MIX_ACK deadbeef 1 256\n",
    b"ARGOS_MIX_ACK a1b2c3d4 2 256\n",
    b"ARGOS_MIX_ACK a1b2c3d4 1 0\n",
    b"ARGOS_MIX_ACK a1b2c3d4 01 256\n",
    b"ARGOS_MIX_READY a1b2c3d4\n",
    b"ARGOS_MIX_IDLE a1b2c3d4 1\n",
    b"ARGOS_MIX_ACK a1b2c3d4 1 256\nARGOS_MIX_ACK a1b2c3d4 99 256\n",
    b"ARGOS_MIX_ACK a1b2c3d4 1 256\nARGOS_MIX_ACK a1b2c3d4 1 256\n",
])
def test_unexpected_ack_or_expiry_is_terminal_including_trailing_batch_message(reply):
    mix, port, _ = make()
    mix.connect()
    port.transform = lambda _: reply
    with pytest.raises(probe.ProbeError):
        mix.set_value(256)
    with pytest.raises(probe.ProbeError):
        mix.set_value(0)
    assert len(port.writes) == 2


def test_repeated_greeting_is_ignored_while_waiting_for_expected_response():
    mix, port, _ = make()
    port.transform = lambda reply: probe.HELLO + b"\n" + reply + probe.HELLO + b"\n"
    mix.connect()
    assert mix.set_value(256) == 1
    assert mix.wait_idle() == 1


def test_early_expiry_during_pacing_stops_before_the_next_write():
    mix, port, _ = make()
    mix.connect()
    mix.set_value(256)
    port.chunks.append(f"ARGOS_MIX_IDLE {SESSION} 1\n".encode())
    with pytest.raises(probe.ProbeError, match="waiting"):
        mix.set_value(-256)
    assert len(port.writes) == 2


@pytest.mark.parametrize("reply", [b"ARGOS_MIX_IDLE deadbeef 1\n",
                                   b"ARGOS_MIX_IDLE a1b2c3d4 2\n"])
def test_idle_must_match_the_current_session_and_last_acknowledged_sequence(reply):
    mix, port, _ = make()
    mix.connect()
    mix.set_value(256)
    port.chunks.append(reply)
    with pytest.raises(probe.ProbeError):
        mix.wait_idle()
    with pytest.raises(probe.ProbeError):
        mix.set_value(0)
    assert len(port.writes) == 2


def test_missing_expiry_stops_without_cleanup_or_followup_phase():
    mix, port, clock = make()
    mix.connect()
    mix.set_value(256)
    port.expiry = None
    with pytest.raises(probe.ProbeError, match="timeout"):
        mix.wait_idle()
    assert clock.now == pytest.approx(2.)
    with pytest.raises(probe.ProbeError):
        mix.set_value(-256)
    assert len(port.writes) == 2


@pytest.mark.parametrize("partial_on", [1, 2])
def test_partial_write_is_terminal_without_retry(partial_on):
    mix, port, _ = make()
    port.partial_on = partial_on
    with pytest.raises(probe.ProbeError, match="partial"):
        mix.connect()
        mix.set_value(256)
    with pytest.raises(probe.ProbeError):
        mix.connect()
    with pytest.raises(probe.ProbeError):
        mix.set_value(0)
    assert len(port.writes) == partial_on


@pytest.mark.parametrize("value", [257, -257, 1, 0., True, "256"])
def test_arbitrary_values_are_refused_before_write(value):
    mix, port, _ = make()
    mix.connect()
    with pytest.raises(probe.ProbeError, match="only"):
        mix.set_value(value)
    assert len(port.writes) == 1


def test_exchange_limit_and_monotonic_sequence():
    mix, port, _ = make()
    mix.connect()
    for sequence in range(1, 121):
        assert mix.set_value(0) == sequence
    with pytest.raises(probe.ProbeError, match="120"):
        mix.set_value(0)
    assert len(port.writes) == 121


def test_cli_runs_finite_pattern_and_closes(monkeypatch, capsys):
    mix, port, _ = make()
    monkeypatch.setattr(probe, "open_port", lambda _: port)
    monkeypatch.setattr(probe, "MixProbe", lambda _: mix)
    assert probe.main(["--port", "unused"]) == 0
    assert port.closed
    assert len(port.writes) == 61
    assert "Completed 60 mixer exchanges and 2 expiry reports" in capsys.readouterr().out


@pytest.mark.parametrize("error,code", [(OSError("unplugged"), 1),
                                      (KeyboardInterrupt(), 130)])
def test_cli_midrun_failure_closes_without_cleanup_writes(monkeypatch, error, code):
    mix, port, _ = make()
    port.error_on = (2, error)
    monkeypatch.setattr(probe, "open_port", lambda _: port)
    monkeypatch.setattr(probe, "MixProbe", lambda _: mix)
    assert probe.main(["--port", "unused"]) == code
    assert port.closed
    assert mix.failed
    assert len(port.writes) == 2


@pytest.mark.parametrize("args", [[], ["--port", ""], ["--port", " ttyACM0"],
                                  ["--port", "unused", "--value", "256"],
                                  ["--port", "unused", "--samples", "999"]])
def test_cli_rejects_missing_port_and_custom_patterns_without_open(monkeypatch, args):
    monkeypatch.setattr(probe, "open_port", lambda *_: pytest.fail("opened a device"))
    with pytest.raises(SystemExit) as exc:
        probe.main(args)
    assert exc.value.code == 2


def test_list_ports_only_lists_metadata(monkeypatch, capsys):
    list_ports = pytest.importorskip("serial.tools.list_ports")
    monkeypatch.setattr(probe, "open_port", lambda *_: pytest.fail("opened a device"))
    monkeypatch.setattr(list_ports, "comports", lambda: [SimpleNamespace(
        device="/dev/example", description="Synthetic USB", vid=1, pid=2)])
    assert probe.main(["--list-ports"]) == 0
    assert '"port": "/dev/example"' in capsys.readouterr().out


def test_standalone_entry_uses_adjacent_display_helper():
    result = subprocess.run([sys.executable, probe.__file__, "--help"],
                            capture_output=True, text=True, timeout=5.)
    assert result.returncode == 0, result.stderr
    assert "--list-ports" in result.stdout
