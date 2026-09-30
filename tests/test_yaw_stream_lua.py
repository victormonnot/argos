"""Targeted contract tests between the real Python sender and real Lua script.

Only clock, physical inputs and serial bytes are substituted. This is not a
hardware timing model or a replay campaign. CI installs Lua; local runs without
Lua explicitly skip these tests rather than substituting a Python radio model.
"""
from contextlib import contextmanager
import itertools
from pathlib import Path
import select
import shutil
import subprocess

import pytest

from argos.backends.edgetx_yaw_stream import SelectionResult, SourceSample, YawStream
from argos.backends.yaw_stream_source import YawDemand


ROOT = Path(__file__).resolve().parents[1]
LUA = shutil.which("lua5.4") or shutil.which("lua")
pytestmark = pytest.mark.skipif(LUA is None, reason="Lua 5.3+ required for actual script tests")


class LuaPort:
    def __init__(self, process):
        self.process = process
        self.received = bytearray()
        self.pending = bytearray()
        self.writes = []
        self.output = (0, 0, 0, 0)

    @property
    def in_waiting(self):
        return len(self.received)

    @property
    def out_waiting(self):
        return 0

    def read(self, maximum):
        chunk = bytes(self.received[:maximum])
        del self.received[:maximum]
        return chunk

    def write(self, packet):
        self.pending.extend(packet)
        self.writes.append(packet)
        return len(packet)

    def callback(self, tick, *, sc=-1024, rud=0, takeover=False, packet=None):
        if packet is None:
            packet = bytes(self.pending)
            self.pending.clear()
        request = f"{tick} {sc} {rud} {int(takeover)} {packet.hex() or '-'}\n"
        self.process.stdin.write(request)
        self.process.stdin.flush()
        assert select.select([self.process.stdout], [], [], 5.)[0], "Lua test driver did not respond"
        response = self.process.stdout.readline().strip()
        assert response, "Lua test driver exited unexpectedly"
        fields = response.split()
        assert len(fields) == 5, response
        self.output = tuple(map(int, fields[:4]))
        if fields[4] != "-":
            self.received.extend(bytes.fromhex(fields[4]))
        return self.output


@contextmanager
def actual_radio():
    process = subprocess.Popen(
        [LUA, "tests/edgetx_yaw_stream_driver.lua"], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        yield LuaPort(process)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=2.)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=2.)
        error = process.stderr.read()
        process.stdout.close()
        process.stderr.close()
        assert process.returncode == 0, error


class Pair:
    def __init__(self, port, *, radio_selection=False):
        self.now = 0.
        self.port = port
        serial = itertools.count(1)
        self.requests = []
        self.selection_result = None
        self.host = YawStream(port, clock=lambda: self.now,
                              nonce=lambda: f"{next(serial):08x}",
                              request_selection=self.requests.append if radio_selection else None)
        self.key = ("run", "camera", 7, 1, "/dev/video2")

    def step(self, tick, *, sc=-1024, rud=0, takeover=False, valid=True):
        self.now = tick / 100
        output = self.port.callback(tick, sc=sc, rud=rud, takeover=takeover)
        demand = YawDemand(75 if valid else 0, valid, self.key,
                           self.now + .4 if valid else self.now,
                           "tracking" if valid else "target_paused")
        self.host.step(SourceSample(demand, self.now, selection_result=self.selection_result))
        return output

    def arm(self):
        for tick in range(0, 20, 5):
            self.step(tick, sc=0)
        for tick in range(20, 60, 5):
            self.step(tick)
        assert self.port.output[:2] == (75, 1024)


def test_actual_lua_stream_exceeds_old_ceiling_and_recovers_pause_and_manual_takeover():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm()
        session = pair.host.session
        for tick in range(60, 4500, 5):
            paused = 200 <= tick < 235
            # Mode 2 incidental yaw and a brief excursion do not latch takeover.
            rud = 300 if 300 <= tick < 310 else 150
            output = pair.step(tick, valid=not paused, rud=rud)
            # Allow the next 10 Hz send slot and the next Lua callback.
            if 215 <= tick < 235:
                assert output[:2] == (0, 0)
            if tick >= 255:
                assert output[:2] == (75, 1024)
        assert pair.host.session == session
        assert pair.host.sent_sequence > 300
        assert not pair.host.failed

        for tick in range(4500, 4540, 5):
            pair.step(tick, rud=300)
        assert port.output[:2] == (0, 0)
        for tick in range(4540, 4570, 5):
            pair.step(tick)
        assert port.output[:2] == (0, 0), "recentering must not restore assistance"
        pair.step(4570, sc=0)
        pair.step(4575, sc=0)
        for tick in range(4580, 4620, 5):
            pair.step(tick)
        assert port.output[:2] == (75, 1024)


def test_actual_lua_rejects_delayed_ticket_and_old_connection_until_operator_cycle():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm()
        # Hold an actual host SET longer than the ticket window, without
        # inventing its fields or granting a new receipt-relative lifetime.
        for tick in range(60, 85, 5):
            pair.step(tick)
        delayed = next(packet for packet in reversed(port.writes) if packet.startswith(b"AS1 "))
        port.pending.clear()
        output = port.callback(125, packet=delayed)
        assert output[:2] == (0, 0)
        # The local native latch covers a large stick takeover between Lua runs.
        output = port.callback(130, takeover=True)
        assert output[:2] == (0, 0)
        for tick in (135, 140):
            assert port.callback(tick, packet=delayed)[:2] == (0, 0)

        # A fresh host process can connect while SC stays up, but cannot enable.
        pair.now = 1.45
        pair.host = YawStream(port, clock=lambda: pair.now,
                              nonce=iter(("1234abcd", "1234abce", "1234abcf")).__next__)
        for tick in range(145, 235, 5):
            assert pair.step(tick)[:2] == (0, 0)
        pair.step(235, sc=0)
        pair.step(240, sc=0)
        for tick in range(245, 285, 5):
            pair.step(tick)
        assert port.output[:2] == (75, 1024)


def test_actual_radio_enable_selects_new_target_once_and_reports_withdrawal_cause():
    with actual_radio() as port:
        pair = Pair(port, radio_selection=True)
        for tick in range(0, 20, 5):
            pair.step(tick, sc=0)
        session = pair.host.session
        pair.step(20)
        assert len(pair.requests) == 1 and pair.host.selection_pending
        token = pair.requests[-1]
        for tick in range(25, 50, 5):
            assert pair.step(tick)[:2] == (0, 0), "pre-enable image cannot acquire authority"

        pair.key = ("run", "camera", 91, 2, "/dev/video2")
        pair.selection_result = SelectionResult(token, pair.now, pair.key, True)
        for tick in range(50, 85, 5):
            pair.step(tick)
        assert port.output[:2] == (75, 1024)
        assert pair.host.session == session and pair.host.selection_key == pair.key
        assert len(pair.requests) == 1

        for tick in range(85, 130, 5):
            pair.step(tick, valid=False)
        assert port.output[:2] == (0, 0)
        assert pair.host.status.cause == "T"
        assert pair.host.snapshot()["radio_a_to_t_causes"] == {"T": 1}
        assert len(pair.requests) == 1, "occlusion does not imitate a physical SC edge"
        for tick in range(130, 160, 5):
            pair.step(tick)
        assert port.output[:2] == (75, 1024)

        # Explicit manual rearm generates a new transaction. The previous
        # selection result is still present but cannot satisfy that new token.
        pair.step(160, sc=0)
        pair.step(165, sc=0)
        pair.step(170)
        assert len(pair.requests) == 2 and pair.requests[-1] != token
        for tick in range(175, 200, 5):
            assert pair.step(tick)[:2] == (0, 0)
        pair.selection_result = SelectionResult(pair.requests[-1], pair.now, pair.key, True)
        for tick in range(200, 235, 5):
            pair.step(tick)
        assert port.output[:2] == (75, 1024) and pair.host.session == session


def test_lua_targeted_policy_harness():
    result = subprocess.run([LUA, "tests/edgetx_yaw_stream_test.lua"], cwd=ROOT,
                            capture_output=True, text=True, timeout=20.)
    assert result.returncode == 0, result.stdout + result.stderr
