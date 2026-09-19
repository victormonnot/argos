"""Read-only MSP wire contract and a finite USB CLI, without real hardware."""
from collections import deque
import errno
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from argos.backends import betaflight_probe as probe


READ_IDS = {1, 2, 3, 4, 105, 108, 119, 150}
SCRIPT = Path(probe.__file__).resolve()


def reply(command, payload=b"", *, rejected=False):
    body = bytes((len(payload), command)) + payload
    checksum = 0
    for value in body:
        checksum ^= value
    return (b"$M!" if rejected else b"$M>") + body + bytes((checksum,))


def string(value):
    encoded = value.encode("ascii")
    return bytes((len(encoded),)) + encoded


def status_payload(*, modes=1 << 2, extra=b"\x80\x01"):
    # API 1.48: prefix, extra-mode count/flags, then arming/config/temperature.
    return (struct.pack("<HHHIBHBBB", 125, 7, 3, modes, 1, 42, 3, 2, len(extra))
            + extra + struct.pack("<BIBHB", 26, 0x00010004, 1, 51, 6))


def controller_payloads():
    """Fabricated board and receiver data, unrelated to any user's backup."""
    return {
        1: bytes((0, 1, 48)),
        2: b"BTFL",
        3: bytes((26, 6, 0)) + string("2026.6.0-test"),
        4: b"TEST" + struct.pack("<HBB", 9, 0, 0)
           + string("STM32_TEST") + string("SYNTHETIC_BOARD") + string("TEST"),
        119: bytes((1, 13, 0, 46)),  # ARM deliberately is not mode bit zero.
        150: status_payload(),
        108: struct.pack("<hhh", -123, 456, -179),
        105: struct.pack("<6H", 1401, 1502, 1603, 1004, 1705, 1806),
    }


class Clock:
    def __init__(self):
        self.now = 100.
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, duration):
        assert duration >= 0
        self.sleeps.append(duration)
        self.now += duration


class Port:
    def __init__(self, payloads=None, *, chunks=None, write_count=None):
        self.payloads = controller_payloads() if payloads is None else payloads
        self.chunks = deque(chunks or [])
        self.write_count = write_count
        self.writes = []
        self.resets = 0
        self.reads = 0
        self.closed = False

    def reset_input_buffer(self):
        self.resets += 1

    def write(self, data):
        self.writes.append(bytes(data))
        assert data[:4] == b"$M<\x00" and len(data) == 6
        assert data[4] in READ_IDS and data[5] == data[4]
        if data[4] in self.payloads:
            self.chunks.append(reply(data[4], self.payloads[data[4]]))
        return len(data) if self.write_count is None else self.write_count

    def read(self, maximum):
        assert maximum <= 4096
        self.reads += 1
        result = self.chunks.popleft() if self.chunks else b""
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        self.closed = True


def make_probe(port, *, timeout=.05):
    clock = Clock()
    return probe.Probe(port, timeout=timeout, clock=clock.clock, sleep=clock.sleep), clock


def test_only_empty_read_commands_can_be_encoded():
    assert {int(command) for command in probe.ReadCommand} == READ_IDS
    for command in probe.ReadCommand:
        assert probe.encode_request(command) == b"$M<\x00" + bytes((command, command))
    # Reject both setters and an untyped integer that happens to be a read ID.
    for command in (1, 200, 202, 214, 250, "RC", b"\x69", None):
        with pytest.raises(ValueError, match="allowlisted"):
            probe.encode_request(command)


def test_request_rejects_non_allowlisted_values_before_touching_the_port():
    port = Port()
    reader, _ = make_probe(port)
    with pytest.raises(ValueError):
        reader.request(200)
    assert port.writes == [] and port.resets == port.reads == 0


def test_parser_recovers_from_noise_and_request_echo_across_every_split():
    wire = b"junk$notMSP$M<\x00\x01\x01" + reply(108, b"$M!\x00\xff") + reply(105)
    for split in range(len(wire) + 1):
        parser = probe.ReplyParser()
        packets = parser.feed(wire[:split]) + parser.feed(wire[split:])
        assert packets == [probe.Reply(108, b"$M!\x00\xff", False), probe.Reply(105, b"", False)]
        assert parser.buffer == b""


def test_parser_handles_one_byte_fragments_and_retains_only_possible_header_noise():
    parser = probe.ReplyParser()
    assert parser.feed(b"x" * 4095 + b"$") == []
    assert parser.buffer == b"$"
    packets = []
    for byte in reply(1, b"\x00\x01\x30")[1:]:
        packets.extend(parser.feed(bytes((byte,))))
    assert packets == [probe.Reply(1, b"\x00\x01\x30", False)]


@pytest.mark.parametrize("wire,message", [
    (reply(108, b"abc")[:-1] + b"\xff", "checksum"),
    (b"$M>\xff\x04", "jumbo"),
    (b"x" * 4097, "4096"),
])
def test_malformed_wire_fails_explicitly(wire, message):
    with pytest.raises(probe.ProbeError, match=message):
        probe.ReplyParser().feed(wire)


def test_controller_error_is_distinct_from_empty_success_and_poisons_connection():
    port = Port({}, chunks=[reply(1, rejected=True)])
    reader, _ = make_probe(port)
    with pytest.raises(probe.ProbeError, match="rejected API_VERSION"):
        reader.request(probe.ReadCommand.API_VERSION)
    with pytest.raises(probe.ProbeError, match="close this connection"):
        reader.request(probe.ReadCommand.API_VERSION)
    assert len(port.writes) == 1


def test_mismatched_replies_do_not_replace_requested_reply():
    port = Port({}, chunks=[reply(105, b"ignore") + reply(1, b"\x00\x01\x30")])
    reader, _ = make_probe(port)
    payload, received = reader.request(probe.ReadCommand.API_VERSION)
    assert payload == b"\x00\x01\x30" and received == 0


@pytest.mark.parametrize("wrong_reply", [b"", reply(105, b"unrelated")])
def test_silence_and_repeated_unrelated_replies_have_one_absolute_deadline(wrong_reply):
    port = Port({}, chunks=[wrong_reply] * 1000)
    reader, clock = make_probe(port)
    with pytest.raises(probe.ProbeError, match="timeout waiting for API_VERSION"):
        reader.request(probe.ReadCommand.API_VERSION)
    assert clock.now - 100 == pytest.approx(.05)
    assert 1 <= port.reads <= 11
    with pytest.raises(probe.ProbeError, match="close this connection"):
        reader.request(probe.ReadCommand.API_VERSION)
    assert len(port.writes) == 1


def test_matching_reply_arriving_after_deadline_is_not_accepted():
    port = Port()
    reader, clock = make_probe(port)
    original_read = port.read

    def delayed_read(maximum):
        clock.now += .051
        return original_read(maximum)

    port.read = delayed_read
    with pytest.raises(probe.ProbeError, match="timeout"):
        reader.request(probe.ReadCommand.API_VERSION)


@pytest.mark.parametrize("written", [0, 3, 5])
def test_partial_write_poisons_connection_without_retry_or_further_reads(written):
    port = Port(write_count=written)
    reader, _ = make_probe(port)
    with pytest.raises(probe.ProbeError, match="partial serial write"):
        reader.request(probe.ReadCommand.API_VERSION)
    with pytest.raises(probe.ProbeError, match="close this connection"):
        reader.request(probe.ReadCommand.RC)
    assert len(port.writes) == 1 and port.reads == 0


def test_identity_decodes_calver_and_length_prefixed_board_strings():
    port = Port()
    reader, _ = make_probe(port)
    identity = reader.identity()
    assert identity["firmware"] == "2026.6.0-test"
    assert identity["firmware_numbers"] == [2026, 6, 0]
    assert identity["board_identifier"] == "TEST"
    assert identity["hardware_revision"] == 9
    assert identity["target"] == "STM32_TEST"
    assert identity["board_name"] == "SYNTHETIC_BOARD"
    assert identity["manufacturer"] == "TEST"
    assert identity["box_ids_page0"] == [1, 13, 0, 46]
    assert [wire[4] for wire in port.writes] == [1, 2, 3, 4, 119]


@pytest.mark.parametrize("api", [b"", b"\x00\x01", b"\x00\x01\x2f", b"\x00\x01\x31", b"\x01\x01\x30", b"\x00\x01\x30\x00"])
def test_unsupported_api_stops_identification_before_further_queries(api):
    port = Port({1: api})
    reader, _ = make_probe(port)
    with pytest.raises(probe.ProbeError, match="unsupported MSP API"):
        reader.identity()
    assert [wire[4] for wire in port.writes] == [1]


@pytest.mark.parametrize("command,payload", [
    (2, b"INAV"),
    (3, b"\x1a\x06\x00"),
    (3, b"\x1a\x06\x00\x04abc"),
    (3, b"\x1a\x06\x00\x01\x00"),
    (4, b"TEST\x00\x00\x00"),
    (4, b"TEST\x00\x00\x00\x00\x09short"),
    (4, b"TEST\x00\x00\x00\x00\x00\x00\x01\xff"),
    (119, b""),
    (119, b"\x01\x02"),
    (119, b"\x00\x01\x00"),
    (119, bytes(range(33))),
])
def test_malformed_or_ambiguous_identity_never_enables_sampling(command, payload):
    payloads = controller_payloads()
    payloads[command] = payload
    port = Port(payloads)
    reader, _ = make_probe(port)
    with pytest.raises(probe.ProbeError):
        reader.identity()
    before = len(port.writes)
    with pytest.raises(probe.ProbeError, match="verify identity"):
        reader.sample()
    assert len(port.writes) == before


def test_status_resolves_arm_via_boxids_not_fixed_bit_zero_and_reads_extended_fields():
    boxes = (1, 13, 0, 46)
    status = probe.decode_status(status_payload(modes=(1 << 2) | (1 << 3)), boxes)
    assert status["armed"] is True
    assert status["active_mode_ids_page0"] == [0, 46]
    assert status["extra_mode_flags_hex"] == "8001"
    assert status["arming_disable_flag_count"] == 26
    assert status["arming_disable_flags"] == 0x00010004
    assert status["configuration_state"] == 1
    assert status["core_temperature_c"] == 51
    assert status["rate_profile_count"] == 6
    assert status["system_load_percent"] == 42
    status = probe.decode_status(status_payload(modes=1, extra=b""), boxes)
    assert status["armed"] is False
    assert status["active_mode_ids_page0"] == [1]
    assert probe.decode_status(status_payload(), (1, 13))["armed"] is None


def test_msp_rc_is_canonical_roll_pitch_yaw_throttle_not_receiver_aetr():
    decoded = probe.decode_rc(struct.pack("<6H", 1401, 1502, 1603, 1004, 1705, 1806))
    assert decoded == {"roll": 1401, "pitch": 1502, "yaw": 1603,
                       "throttle": 1004, "aux": [1705, 1806]}
    assert probe.decode_attitude(struct.pack("<hhh", -123, 456, -179)) == {
        "roll_deg": -12.3, "pitch_deg": 45.6, "yaw_deg": -179}


@pytest.mark.parametrize("payload", [b"", b"\x00" * 6, b"\x00" * 9, b"\x00" * 38])
def test_incomplete_or_out_of_range_rc_channel_count_is_rejected(payload):
    with pytest.raises(probe.ProbeError, match="channels"):
        probe.decode_rc(payload)


def test_truncated_status_or_attitude_never_becomes_a_valid_sample():
    wire = status_payload()
    for length in range(len(wire)):
        with pytest.raises(probe.ProbeError, match="truncated"):
            probe.decode_status(wire[:length], (1, 13, 0))
    for length in range(6):
        with pytest.raises(probe.ProbeError, match="truncated"):
            probe.decode_attitude(b"\x00" * length)


@pytest.mark.parametrize("args", [
    [], ["--port", ""], ["--port", " /dev/fake"],
    ["--port", "/dev/fake", "--samples", "0"],
    ["--port", "/dev/fake", "--samples", "301"],
    ["--port", "/dev/fake", "--interval", "nan"],
    ["--port", "/dev/fake", "--interval", ".1"],
    ["--port", "/dev/fake", "--timeout", "inf"],
    ["--port", "/dev/fake", "--timeout", "0"],
    ["--port", "/dev/fake", "--list-ports"],
])
def test_invalid_cli_arguments_are_rejected_before_opening_any_device(monkeypatch, args):
    monkeypatch.setattr(probe, "open_port", lambda *a, **kw: pytest.fail("device opened"))
    with pytest.raises(SystemExit) as exc:
        probe.main(args)
    assert exc.value.code == 2


def test_port_listing_only_reads_metadata(monkeypatch, capsys):
    list_ports = pytest.importorskip("serial.tools.list_ports")
    devices = [SimpleNamespace(device="/dev/fakeB", description="Synthetic B", vid=None, pid=None),
               SimpleNamespace(device="/dev/fakeA", description="Synthetic A", vid=1, pid=2)]
    monkeypatch.setattr(list_ports, "comports", lambda: devices)
    monkeypatch.setattr(probe, "open_port", lambda *a, **kw: pytest.fail("device opened"))
    assert probe.main(["--list-ports"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["port"] for line in lines] == ["/dev/fakeA", "/dev/fakeB"]


@pytest.mark.parametrize("failure,exit_code", [(None, 0), (OSError("disconnected"), 1), (KeyboardInterrupt(), 130)])
def test_cli_closes_serial_on_success_error_and_interrupt(monkeypatch, capsys, failure, exit_code):
    port = Port(chunks=[failure] if failure is not None else [])
    monkeypatch.setattr(probe, "open_port", lambda *a, **kw: port)
    assert probe.main(["--port", "/dev/fake", "--samples", "1"]) == exit_code
    output = capsys.readouterr()
    assert port.closed
    if failure is None:
        assert "Completed 1 samples." in output.out
        assert "RC R/P/Y/T 1401/1502/1603/1004" in output.out
    else:
        assert "Completed" not in output.out
        assert "stopped" in output.err or "interrupted" in output.err


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux USB-like PTY integration")
def test_standalone_cli_reads_two_samples_over_pty_and_emits_no_control_commands(tmp_path):
    pytest.importorskip("serial")
    import pty

    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--port", slave_name,
         "--samples", "2", "--interval", ".2", "--timeout", ".5", "--json"],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    os.close(slave)
    raw_outbound = bytearray()
    pending = bytearray()
    commands = []
    payloads = controller_payloads()
    deadline = time.monotonic() + 8
    try:
        while process.poll() is None and time.monotonic() < deadline:
            if not select.select([master], [], [], .05)[0]:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                # No slave yet during startup, or already closed at completion.
                time.sleep(.005)
                continue
            raw_outbound.extend(chunk)
            pending.extend(chunk)
            while len(pending) >= 6:
                request = bytes(pending[:6])
                del pending[:6]
                # Reject arbitrary data, configuration writes and nonempty reads.
                assert request[:4] == b"$M<\x00", request
                command = request[4]
                assert command in READ_IDS and request[5] == command, request
                commands.append(command)
                os.write(master, reply(command, payloads[command]))
        stdout, stderr = process.communicate(timeout=2)
        assert process.returncode == 0, stderr
        assert pending == b""
        assert commands == [1, 2, 3, 4, 119, 150, 108, 105, 150, 108, 105]
        assert bytes(raw_outbound) == b"".join(b"$M<\x00" + bytes((cmd, cmd)) for cmd in commands)
        records = [json.loads(line) for line in stdout.splitlines()]
        assert [record["type"] for record in records] == ["identity", "sample", "sample", "end"]
        assert records[0]["firmware"] == "2026.6.0-test"
        assert records[1]["status"]["armed"] is True
        assert records[1]["attitude"]["pitch_deg"] == 45.6
        assert records[1]["rc"]["yaw"] == 1603
        assert records[1]["rc"]["throttle"] == 1004
        assert records[2]["rc"]["received_after_s"] >= records[1]["rc"]["received_after_s"] + .19
        assert records[-1] == {"type": "end", "samples": 2}
        assert "USB bench readout only" in stderr
        assert "processed controller values" in stderr
        # No slave remains open after the CLI terminates.
        with pytest.raises(OSError) as closed:
            os.read(master, 4096)
        assert closed.value.errno == errno.EIO
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)
        os.close(master)
