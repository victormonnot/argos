"""Small real host/Lua contract checks; not a hardware timing simulation."""
from contextlib import contextmanager
import itertools
from pathlib import Path
import select
import shutil
import subprocess

import pytest

from argos.backends.edgetx_distance_stream import DistanceStream, SourceSample
from argos.backends.distance_source import DistanceDemand

ROOT = Path(__file__).resolve().parents[1]
LUA = shutil.which("lua5.4") or shutil.which("lua")
pytestmark = pytest.mark.skipif(LUA is None, reason="Lua 5.3+ required for actual script tests")


class Port:
    def __init__(self, process):
        self.process = process
        self.received = bytearray()
        self.pending = bytearray()
        self.writes = []
        self.output = (0,) * 6

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

    def callback(self, tick, *, sc=1024, ele=0, rud=0, ail=0, thr=-1024, sb=-1024, packet=None):
        if packet is None:
            packet = bytes(self.pending)
            self.pending.clear()
        request = f"{tick} {sc} {rud} {ele} {ail} {thr} {sb} 0 {packet.hex() or '-'}\n"
        self.process.stdin.write(request)
        self.process.stdin.flush()
        assert select.select([self.process.stdout], [], [], 5.)[0], "Lua driver did not respond"
        fields = self.process.stdout.readline().strip().split()
        assert len(fields) == 7, fields
        self.output = tuple(map(int, fields[:6]))
        if fields[6] != "-":
            self.received.extend(bytes.fromhex(fields[6]))
        return self.output


@contextmanager
def actual_radio():
    process = subprocess.Popen([LUA, "tests/edgetx_distance_driver.lua"], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        yield Port(process)
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
    def __init__(self, port):
        self.now, self.port = 0., port
        serial = itertools.count(1)
        self.host = DistanceStream(port, clock=lambda: self.now,
                                   nonce=lambda: f"{next(serial):08x}")
        self.key = ("run", "camera", 7, 1, "/dev/video2")

    def step(self, tick, *, sc=1024, ele=0, rud=0, ail=0, thr=-1024, sb=-1024,
             valid=True, pitch_valid=True, mode="D"):
        self.now = tick / 100
        output = self.port.callback(tick, sc=sc, ele=ele, rud=rud, ail=ail, thr=thr, sb=sb)
        demand = DistanceDemand(75, valid, self.key, self.now + .4, "tracking",
                                pitch=40, pitch_valid=pitch_valid, mode=mode)
        self.host.step(SourceSample(demand, self.now))
        return output

    def arm(self, mode="D"):
        for tick in range(0, 20, 5):
            self.step(tick, sc=0, mode=mode)
        for tick in range(20, 70, 5):
            self.step(tick, sc=1024 if mode == "D" else -1024, mode=mode)
        assert self.port.output[:2] == (75, 1024)
        assert self.port.output[4:] == ((40, 1024) if mode == "D" else (0, 0))


def test_real_pilot_sample_distinguishes_axis_takeover_invalid_pitch_and_lease_expiry():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm()
        for tick in range(70, 120, 5):
            pair.step(tick, rud=-1024, ele=20, ail=-587, thr=813)
        pilot = pair.host.snapshot()["pilot_sample"]
        assert pilot["sticks"] == dict(roll=-587, pitch=20, throttle=813, yaw=-1024)
        assert pilot["state"] == "A" and pilot["mode"] == "D" and pilot["yaw_phase"] == "M"
        assert pilot["lua_outputs"] == {"yaw": dict(valid=False, value=0),
                                         "pitch": dict(valid=True, value=40)}
        for tick in range(120, 175, 5):
            pair.step(tick, pitch_valid=False)
        pilot = pair.host.snapshot()["pilot_sample"]
        assert pilot["state"] == "A" and pilot["pitch_phase"] == "A"
        assert pilot["lua_outputs"] == {"yaw": dict(valid=True, value=75),
                                         "pitch": dict(valid=False, value=0)}
        # No fresh host packet: callback expiry is observed, never converted
        # into valid output just because the last accepted sequence is nonzero.
        port.callback(220, packet=b"")
        pair.now = 2.2
        pair.host._receive(pair.now)
        pilot = pair.host.snapshot()["pilot_sample"]
        assert pilot["cause"] == "E" and pilot["ack"] > 0
        assert not pilot["lua_outputs"]["yaw"]["valid"]
        assert not pilot["lua_outputs"]["pitch"]["valid"]


def test_real_python_and_lua_distance_validity_and_full_pitch_resume():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm()
        for tick in range(70, 150, 5):
            pair.step(tick, pitch_valid=False)
        assert port.output[:2] == (75, 1024) and port.output[4:] == (0, 0)
        for tick in range(150, 200, 5):
            pair.step(tick)
        assert port.output[4:] == (40, 1024)
        original = pair.host.session, pair.host.status.generation, pair.host.selection_key
        for tick in range(200, 350, 5):
            pair.step(tick, ele=-1024)
            assert port.output[:2] == (75, 1024) and port.output[4:] == (0, 0)
            assert pair.host.status.state == "A"
        for tick in range(350, 370, 5):
            pair.step(tick)
            assert port.output[4:] == (0, 0)
        for tick in range(370, 420, 5):
            pair.step(tick)
        assert port.output[:2] == (75, 1024) and port.output[4:] == (40, 1024)
        assert (pair.host.session, pair.host.status.generation, pair.host.selection_key) == original


def test_real_radio_yaw_mode_never_accepts_pitch_and_direct_mode_change_returns_manual():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm(mode="Y")
        for tick in range(70, 110, 5):
            pair.step(tick, sc=-1024, mode="Y", ele=400)
        assert port.output[:2] == (75, 1024) and port.output[4:] == (0, 0)
        for tick in range(110, 150, 5):
            pair.step(tick, mode="D")
        assert port.output[:2] == (0, 0) and port.output[4:] == (0, 0)
        pair.step(150, sc=0)
        pair.step(155, sc=0)
        for tick in range(160, 210, 5):
            pair.step(tick)
        assert port.output[4:] == (40, 1024)


def test_real_gentle_pitch_releases_only_pitch_then_resumes_without_reselection():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm()
        original = pair.host.session, pair.host.status.generation, pair.host.selection_key
        for tick in range(70, 130, 5):
            pair.step(tick, ele=180)
            assert port.output[:2] == (75, 1024)
            assert port.output[4:] == (0, 0)
        assert pair.host.snapshot()["radio_pitch_phase"] == "M"
        for tick in range(130, 150, 5):
            pair.step(tick)
            assert port.output[4:] == (0, 0)
        for tick in range(150, 185, 5):
            pair.step(tick)
        assert port.output[:2] == (75, 1024) and port.output[4:] == (40, 1024)
        assert pair.host.snapshot()["radio_pitch_phase"] == "A"
        assert (pair.host.session, pair.host.status.generation, pair.host.selection_key) == original


def test_real_delayed_ticket_does_not_refresh_distance_output():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm()
        for tick in range(70, 90, 5):
            pair.step(tick)
        delayed = next(packet for packet in reversed(port.writes) if packet.startswith(b"DS3 "))
        port.pending.clear()
        output = port.callback(130, packet=delayed)
        assert output[:2] == (0, 0) and output[4:] == (0, 0)
        assert b" T E D A A\n" in port.received


@pytest.mark.parametrize("mode", ["Y", "D"])
@pytest.mark.parametrize("sign", [-1, 1])
def test_real_mode2_incidental_yaw_and_wider_recenter_preserve_same_session(mode, sign):
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm(mode=mode)
        sc = 1024 if mode == "D" else -1024
        original = pair.host.session, pair.host.status.generation, pair.host.selection_key
        expected_pitch = (40, 1024) if mode == "D" else (0, 0)
        for tick in range(70, 120, 5):
            pair.step(tick, rud=sign * 150, thr=-1024 + (tick - 70) * 40, sc=sc, mode=mode)
            assert port.output[:2] == (75, 1024)
            assert port.output[4:] == expected_pitch
        assert pair.host.snapshot()["pilot_sample"]["yaw_phase"] == "A"
        for tick in range(120, 140, 5):
            pair.step(tick, rud=sign * 206, sc=sc, mode=mode)
            assert port.output[:2] == (0, 0)
        for tick in range(140, 160, 5):
            pair.step(tick, rud=sign * 102, sc=sc, mode=mode)
            assert port.output[:2] == (0, 0)
        for tick in range(160, 210, 5):
            pair.step(tick, rud=sign * 102, sc=sc, mode=mode)
        assert port.output[:2] == (75, 1024) and port.output[4:] == expected_pitch
        assert pair.host.snapshot()["radio_yaw_phase"] == "A"
        assert (pair.host.session, pair.host.status.generation, pair.host.selection_key) == original


@pytest.mark.parametrize("mode", ["Y", "D"])
@pytest.mark.parametrize("sign", [-1, 1])
def test_real_full_yaw_override_keeps_distance_and_resumes_same_session(mode, sign):
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm(mode=mode)
        sc = 1024 if mode == "D" else -1024
        original = pair.host.session, pair.host.status.generation, pair.host.selection_key
        expected_pitch = (40, 1024) if mode == "D" else (0, 0)
        for tick in range(70, 230, 5):
            pair.step(tick, rud=sign * 1024, sc=sc, mode=mode)
            assert port.output[:2] == (0, 0)
            assert port.output[4:] == expected_pitch
            assert pair.host.status.state == "A"
        for tick in range(230, 250, 5):
            pair.step(tick, sc=sc, mode=mode)
            assert port.output[:2] == (0, 0)
        for tick in range(250, 290, 5):
            pair.step(tick, sc=sc, mode=mode)
        assert port.output[:2] == (75, 1024) and port.output[4:] == expected_pitch
        assert pair.host.snapshot()["radio_yaw_phase"] == "A"
        assert (pair.host.session, pair.host.status.generation, pair.host.selection_key) == original


def test_real_both_sticks_full_then_independent_resume_and_sc_manual():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm()
        original = pair.host.session, pair.host.status.generation, pair.host.selection_key
        for tick in range(70, 230, 5):
            pair.step(tick, rud=1024, ele=1024)
            assert port.output[:2] == (0, 0) and port.output[4:] == (0, 0)
        for tick in range(230, 290, 5):
            pair.step(tick, ele=1024)
        assert port.output[:2] == (75, 1024) and port.output[4:] == (0, 0)
        for tick in range(290, 350, 5):
            pair.step(tick)
        assert port.output[:2] == (75, 1024) and port.output[4:] == (40, 1024)
        assert (pair.host.session, pair.host.status.generation, pair.host.selection_key) == original
        for tick in range(350, 380, 5):
            pair.step(tick, sc=0)
        assert port.output[:2] == (0, 0) and port.output[4:] == (0, 0)
        assert pair.host.status.state == "M"
