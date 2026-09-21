"""Two-port RF bench: real MSP framing, synthetic clocks and terminal guards."""
from collections import deque
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from argos.backends import edgetx_rf_probe as probe


SESSION = "a1b2c3d4"
READ_IDS = {1, 2, 3, 4, 105, 119, 150}


def string(text):
    raw = text.encode("ascii")
    return bytes((len(raw),)) + raw


def packet(command, payload):
    body = bytes((len(payload), command)) + payload
    checksum = 0
    for byte in body:
        checksum ^= byte
    return b"$M>" + body + bytes((checksum,))


class Clock:
    def __init__(self):
        self.now = 100.

    def clock(self):
        return self.now

    def sleep(self, duration):
        assert duration >= 0
        self.now += duration


class Controller:
    """Fabricated replies matching public board identifiers, not a user dump."""

    def __init__(self, clock):
        self.clock = clock
        self.channels = [1500, 1500, 1498, 989, 1000, 1000, 1000, 1000]
        self.flags = 1 << 20
        self.flag_count = 29
        self.armed = False
        self.delay = {150: .002, 105: .002}
        self.writes = []
        self.chunks = deque()
        self.closed = False
        self.silent = set()
        self.error = None
        self.partial = False
        self.override = {}
        self.pending_command = None

    def payload(self, command):
        if command in self.override:
            return self.override[command]
        if command == 150:
            return (struct.pack("<HHHIBHBBB", 125, 0, 3, (1 << 2) if self.armed else 0,
                                0, 40, 3, 1, 0)
                    + struct.pack("<BIBHB", self.flag_count, self.flags, 1, 50, 6))
        if command == 105:
            return struct.pack("<" + "H" * len(self.channels), *self.channels)
        return {
            1: bytes((0, 1, 48)), 2: b"BTFL",
            3: bytes((26, 6, 0)) + string("2026.6.0-alpha"),
            4: b"TEST" + struct.pack("<HBB", 1, 0, 0)
               + string("STM32G47X") + string("BETAFPVG473_V2") + string("BEFH"),
            119: bytes((1, 13, 0, 46)),  # ARM is deliberately not bit zero.
        }[command]

    def reset_input_buffer(self):
        self.chunks.clear()

    def write(self, data):
        assert len(data) == 6 and data[:4] == b"$M<\x00"
        command = data[4]
        assert command in READ_IDS and data[5] == command
        self.writes.append(data)
        self.pending_command = command
        if self.error:
            raise self.error
        if command not in self.silent:
            self.chunks.append(packet(command, self.payload(command)))
        return len(data) - int(self.partial)

    def read(self, maximum):
        assert maximum == 4096
        if self.chunks:
            self.clock.now += self.delay.get(self.pending_command, 0.)
            return self.chunks.popleft()
        return b""

    def close(self):
        self.closed = True


class Pocket:
    def __init__(self, clock, fc):
        self.clock = clock
        self.fc = fc
        self.chunks = deque([(clock.now, probe.HELLO + b"\n")])
        self.writes = []
        self.write_times = []
        self.closed = False
        self.session = None
        self.sequence = None
        self.expiry = None
        self.ack_delay = 0.
        self.write_delay = 0.
        self.error = None
        self.partial = False
        self.transform = lambda response: response
        self.suppress_idle = False
        self.write_timeout = 1.

    def reset_input_buffer(self):
        # Greeting represents fresh post-open input, not stale buffered bytes.
        pass

    def write(self, data):
        self.writes.append(data)
        self.write_times.append(self.clock.now)
        self.clock.now += self.write_delay
        if self.error:
            raise self.error
        if self.partial:
            return len(data) - 1
        fields = data.decode().split()
        if fields[0] == "ARGOS_RF_BEGIN":
            _, self.session = fields
            response = f"ARGOS_RF_READY {self.session}\n".encode()
        else:
            assert fields[0] == "ARGOS_RF_SET"
            _, session, sequence, value = fields
            assert session == self.session
            self.sequence = int(sequence)
            self.expiry = self.clock.now + .3
            self.fc.channels[2] = 1500 + round(int(value) * 500 / 1024)
            response = f"ARGOS_RF_ACK {session} {sequence} {value}\n".encode()
        self.chunks.append((self.clock.now + self.ack_delay, self.transform(response)))
        return len(data)

    def read(self, maximum):
        assert maximum == 256
        if self.chunks and self.chunks[0][0] <= self.clock.now:
            return self.chunks.popleft()[1]
        if self.expiry is not None and self.clock.now >= self.expiry and not self.suppress_idle:
            self.expiry = None
            self.fc.channels[2] = 1498
            return f"ARGOS_RF_IDLE {self.session} {self.sequence}\n".encode()
        return b""

    def close(self):
        self.closed = True


def make():
    clock = Clock()
    fc = Controller(clock)
    pocket = Pocket(clock, fc)
    bench = probe.RFBench(pocket, fc, clock=clock.clock, sleep=clock.sleep)
    bench.session = SESSION
    return bench, pocket, fc, clock


def assert_stopped(bench, pocket):
    before = list(pocket.writes)
    with pytest.raises(probe.ProbeError):
        bench.set_value(0)
    assert pocket.writes == before
    assert bench.failed


def test_fixed_pattern_checks_controller_before_every_set_and_observes_expiry(capsys):
    bench, pocket, fc, _ = make()
    bench.connect()
    probe.run_pattern(bench)
    values = [0] * 10 + [128] * 20 + [-128] * 20 + [0] * 10
    assert pocket.writes == [f"ARGOS_RF_BEGIN {SESSION}\n".encode()] + [
        f"ARGOS_RF_SET {SESSION} {i} {value}\n".encode()
        for i, value in enumerate(values, 1)
    ]
    commands = [wire[4] for wire in fc.writes]
    assert commands[:5] == [1, 2, 3, 4, 119]
    assert commands[5:] == [150, 105] * ((len(commands) - 5) // 2)
    assert commands.count(150) >= 64
    times = pocket.write_times[1:]
    assert all(b - a >= .1 - 1e-9 for a, b in zip(times, times[1:]))
    assert times[30] - times[29] >= .8
    output = capsys.readouterr().out
    assert "FC sampled BEFORE this SET" in output
    assert "1500/1500/1562/989" in output
    assert "1500/1500/1438/989" in output
    for line in output.splitlines():
        if line.startswith("IDLE"):
            assert "1500/1500/1498/989" in line
    assert "IDLE 1/2 after SET 30" in output
    assert "IDLE 2/2 after SET 60" in output


@pytest.mark.parametrize("command,payload", [
    (1, bytes((0, 1, 47))), (2, b"INAV"),
    (3, bytes((26, 6, 0)) + string("2026.6.0")),
    (3, bytes((26, 6, 1)) + string("2026.6.0-alpha")),
    (4, b"TEST" + bytes(4) + string("STM32G47X") + string("OTHER") + string("BEFH")),
    (4, b"TEST" + bytes(4) + string("STM32G47X") + string("BETAFPVG473_V2") + string("TEST")),
    (119, bytes((1, 2, 3))),
])
def test_wrong_identity_prevents_even_begin(command, payload):
    bench, pocket, fc, _ = make()
    fc.override[command] = payload
    with pytest.raises((probe.ProbeError, probe.msp.ProbeError)):
        bench.connect()
    assert pocket.writes == []
    assert_stopped(bench, pocket)


@pytest.mark.parametrize("noise", [b"", b"CLI> ", b"ARGOS_USB_MIX_BENCH_V1\n"])
def test_wrong_pocket_greeting_never_writes(noise):
    bench, pocket, _, clock = make()
    pocket.chunks = deque([(clock.now, noise)])
    with pytest.raises(probe.ProbeError, match="USB-VCP=Lua"):
        bench.connect()
    assert pocket.writes == []
    assert_stopped(bench, pocket)


@pytest.mark.parametrize("phase", ["preflight", "active"])
@pytest.mark.parametrize("hazard", ["armed", "rxloss", "throttle_flag", "cms", "reboot",
                                    "arm_switch", "unknown_flag", "layout", "throttle",
                                    "arm_channel", "flip_channel", "invalid_yaw", "short_aux",
                                    "roll", "pitch"])
def test_any_unsafe_readout_stops_all_future_sets(phase, hazard):
    bench, pocket, fc, _ = make()
    if phase == "active":
        bench.connect()
        bench.set_value(128)
    prior = list(pocket.writes)
    masks = {"rxloss": 2, "throttle_flag": 7, "cms": 14, "reboot": 21,
             "arm_switch": 28, "unknown_flag": 31}
    if hazard in masks:
        fc.flags |= 1 << masks[hazard]
    elif hazard == "armed":
        fc.armed = True
    elif hazard == "layout":
        fc.flag_count = 28
    elif hazard == "throttle":
        fc.channels[3] = 1051
    elif hazard == "arm_channel":
        fc.channels[4] = 2000
    elif hazard == "flip_channel":
        fc.channels[6] = 2000
    elif hazard == "invalid_yaw":
        fc.channels[2] = 899
    elif hazard == "short_aux":
        fc.channels = fc.channels[:6]
    elif hazard == "roll":
        fc.channels[0] = 1551
    elif hazard == "pitch":
        fc.channels[1] = 1449
    with pytest.raises(probe.ProbeError):
        if phase == "preflight":
            bench.connect()
        else:
            bench.set_value(-128)
    assert pocket.writes == prior
    assert_stopped(bench, pocket)


def test_zero_arming_blockers_is_allowed():
    bench, pocket, fc, _ = make()
    fc.flags = 0
    bench.connect()
    bench.set_value(0)
    assert len(pocket.writes) == 2


@pytest.mark.parametrize("command", [150, 105])
def test_missing_fresh_msp_reply_stops_before_next_set(command):
    bench, pocket, fc, _ = make()
    bench.connect()
    bench.set_value(128)
    fc.silent.add(command)
    before = len(pocket.writes)
    with pytest.raises(probe.msp.ProbeError, match="timeout"):
        bench.set_value(-128)
    assert len(pocket.writes) == before
    assert_stopped(bench, pocket)


def test_combined_safety_batch_over_150ms_is_stale_even_if_requests_individually_pass():
    bench, pocket, fc, _ = make()
    bench.connect()
    fc.delay = {150: .08, 105: .08}
    with pytest.raises(probe.ProbeError, match="stale"):
        bench.set_value(128)
    assert len(pocket.writes) == 1
    assert_stopped(bench, pocket)


def test_freshness_is_rechecked_immediately_before_write(monkeypatch):
    bench, pocket, _, clock = make()
    bench.connect()
    real_safety = bench._safety
    def stale():
        sample = real_safety()
        clock.now += .151
        return sample
    monkeypatch.setattr(bench, "_safety", stale)
    with pytest.raises(probe.ProbeError, match="stale"):
        bench.set_value(128)
    assert len(pocket.writes) == 1


def test_delayed_ack_does_not_allow_reviving_past_previous_set_gap():
    bench, pocket, fc, _ = make()
    bench.connect()
    pocket.ack_delay = .18
    bench.set_value(128)
    # Each request and this whole batch still meet their individual limits.
    fc.delay = {150: .04, 105: .04}
    with pytest.raises(probe.ProbeError, match="250 ms"):
        bench.set_value(-128)
    assert len(pocket.writes) == 2
    assert_stopped(bench, pocket)


@pytest.mark.parametrize("reply", [
    b"", b"ARGOS_RF_ACK deadbeef 1 128\n", b"ARGOS_RF_ACK a1b2c3d4 2 128\n",
    b"ARGOS_RF_ACK a1b2c3d4 1 0\n", b"ARGOS_RF_READY a1b2c3d4\n",
    b"ARGOS_RF_IDLE a1b2c3d4 1\n",
    b"ARGOS_RF_ACK a1b2c3d4 1 128\nARGOS_RF_ACK a1b2c3d4 1 128\n",
    b"ARGOS_RF_ACK a1b2c3d4 1 128\nARGOS_RF_IDLE a1b2c3d4 1\n",
])
def test_wrong_or_combined_pocket_responses_fail_terminally(reply):
    bench, pocket, _, _ = make()
    bench.connect()
    pocket.transform = lambda _: reply
    with pytest.raises(probe.ProbeError):
        bench.set_value(128)
    assert len(pocket.writes) == 2
    assert_stopped(bench, pocket)


@pytest.mark.parametrize("reply", [b"", b"ARGOS_RF_READY deadbeef\n"])
def test_unmatched_ready_allows_no_set(reply):
    bench, pocket, _, _ = make()
    pocket.transform = lambda _: reply
    with pytest.raises(probe.ProbeError):
        bench.connect()
    assert pocket.writes == [f"ARGOS_RF_BEGIN {SESSION}\n".encode()]
    assert_stopped(bench, pocket)


def test_active_ack_deadline_is_200ms():
    bench, pocket, _, clock = make()
    bench.connect()
    pocket.ack_delay = .201
    before = clock.now
    with pytest.raises(probe.ProbeError, match="timeout"):
        bench.set_value(128)
    assert clock.now - before == pytest.approx(.204)
    assert_stopped(bench, pocket)


@pytest.mark.parametrize("hazard", ["rxloss", "missing_idle"])
def test_expiry_wait_is_monitored_and_never_resumes_on_failure(hazard):
    bench, pocket, fc, _ = make()
    bench.connect()
    bench.set_value(128)
    if hazard == "rxloss":
        fc.flags |= 1 << 2
    else:
        pocket.suppress_idle = True
    with pytest.raises(probe.ProbeError):
        bench.wait_idle()
    assert len(pocket.writes) == 2
    assert_stopped(bench, pocket)


@pytest.mark.parametrize("error", [OSError("unplugged"), KeyboardInterrupt()])
@pytest.mark.parametrize("device", ["fc", "pocket"])
def test_active_io_failure_poisoning_has_no_cleanup_set(error, device):
    bench, pocket, fc, _ = make()
    bench.connect()
    bench.set_value(128)
    if device == "fc":
        fc.error = error
    else:
        pocket.error = error
    with pytest.raises(type(error)):
        bench.set_value(-128)
    assert_stopped(bench, pocket)


def test_partial_pocket_write_stops_without_retry():
    bench, pocket, _, _ = make()
    bench.connect()
    pocket.partial = True
    with pytest.raises(probe.ProbeError, match="partial"):
        bench.set_value(128)
    assert len(pocket.writes) == 2
    assert_stopped(bench, pocket)


def test_slow_pocket_write_stops_without_further_commands():
    bench, pocket, _, _ = make()
    bench.connect()
    pocket.write_delay = .151
    with pytest.raises(probe.ProbeError, match="serial write exceeded"):
        bench.set_value(128)
    assert len(pocket.writes) == 2
    assert_stopped(bench, pocket)


@pytest.mark.parametrize("value", [-129, 129, 1, True, 0.])
def test_arbitrary_values_are_unavailable(value):
    bench, pocket, _, _ = make()
    bench.connect()
    with pytest.raises(probe.ProbeError, match="permits only"):
        bench.set_value(value)
    assert len(pocket.writes) == 1


def test_sequence_cap():
    bench, pocket, _, _ = make()
    bench.connect()
    for number in range(1, 121):
        assert bench.set_value(0)[0] == number
    with pytest.raises(probe.ProbeError, match="120"):
        bench.set_value(0)
    assert len(pocket.writes) == 121


def test_device_aliases_are_rejected_before_open(tmp_path, monkeypatch):
    device = tmp_path / "device"
    device.touch()
    alias = tmp_path / "alias"
    alias.symlink_to(device)
    monkeypatch.setattr(probe, "open_port", lambda *_: pytest.fail("opened Pocket"))
    monkeypatch.setattr(probe.msp, "open_port", lambda *_, **__: pytest.fail("opened FC"))
    with pytest.raises(SystemExit) as exc:
        probe.main(["--pocket-port", str(alias), "--fc-port", str(device)])
    assert exc.value.code == 2


def test_samefile_hardlink_is_rejected(tmp_path):
    device = tmp_path / "device"
    device.touch()
    alias = tmp_path / "alias"
    alias.hardlink_to(device)
    with pytest.raises(ValueError, match="different"):
        probe.validate_distinct_ports(str(alias), str(device))


@pytest.mark.parametrize("args", [[], ["--pocket-port", "unused"],
                                  ["--pocket-port", "same", "--fc-port", "same"],
                                  ["--pocket-port", " p", "--fc-port", "f"],
                                  ["--pocket-port", "p", "--fc-port", "f", "--value", "128"]])
def test_invalid_cli_has_no_device_side_effects(monkeypatch, args):
    monkeypatch.setattr(probe, "open_port", lambda *_: pytest.fail("opened Pocket"))
    monkeypatch.setattr(probe.msp, "open_port", lambda *_, **__: pytest.fail("opened FC"))
    with pytest.raises(SystemExit) as exc:
        probe.main(args)
    assert exc.value.code == 2


def install_ports(monkeypatch, bench, pocket, fc):
    monkeypatch.setattr(probe.msp, "open_port", lambda _, timeout: fc)
    monkeypatch.setattr(probe, "open_port", lambda _: pocket)
    monkeypatch.setattr(probe, "RFBench", lambda *args: bench)


def test_cli_success_closes_both_and_sets_finite_write_timeout(monkeypatch, capsys):
    bench, pocket, fc, _ = make()
    install_ports(monkeypatch, bench, pocket, fc)
    assert probe.main(["--pocket-port", "p", "--fc-port", "f"]) == 0
    assert pocket.closed and fc.closed
    assert pocket.write_timeout == .15
    assert len(pocket.writes) == 61
    assert "ACKs alone do not prove aircraft response" in capsys.readouterr().out


def test_second_port_open_failure_closes_first_port(monkeypatch):
    _, _, fc, _ = make()
    monkeypatch.setattr(probe.msp, "open_port", lambda _, timeout: fc)
    def fail(_):
        raise OSError("Pocket missing")
    monkeypatch.setattr(probe, "open_port", fail)
    assert probe.main(["--pocket-port", "p", "--fc-port", "f"]) == 1
    assert fc.closed


@pytest.mark.parametrize("error,code", [(OSError("unplugged"), 1), (KeyboardInterrupt(), 130)])
def test_cli_failure_closes_both_ports_without_cleanup(monkeypatch, error, code):
    bench, pocket, fc, _ = make()
    install_ports(monkeypatch, bench, pocket, fc)
    pocket.error = error
    assert probe.main(["--pocket-port", "p", "--fc-port", "f"]) == code
    assert pocket.closed and fc.closed
    assert pocket.writes == [f"ARGOS_RF_BEGIN {SESSION}\n".encode()]


def test_standalone_entry_imports_adjacent_helpers():
    result = subprocess.run([sys.executable, str(Path(probe.__file__)), "--help"],
                            capture_output=True, text=True, timeout=5.)
    assert result.returncode == 0, result.stderr
    assert "--pocket-port" in result.stdout and "--fc-port" in result.stdout
