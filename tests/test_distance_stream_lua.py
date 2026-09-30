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

    def callback(self, tick, *, sc=1024, ele=0, sb=-1024, packet=None):
        if packet is None:
            packet = bytes(self.pending)
            self.pending.clear()
        request = f"{tick} {sc} 0 {ele} {sb} 0 {packet.hex() or '-'}\n"
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

    def step(self, tick, *, sc=1024, ele=0, sb=-1024, valid=True, pitch_valid=True, mode="D"):
        self.now = tick / 100
        output = self.port.callback(tick, sc=sc, ele=ele, sb=sb)
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


def test_real_python_and_lua_distance_validity_and_pitch_takeover():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm()
        for tick in range(70, 150, 5):
            pair.step(tick, pitch_valid=False)
        assert port.output[:2] == (75, 1024) and port.output[4:] == (0, 0)
        for tick in range(150, 200, 5):
            pair.step(tick)
        assert port.output[4:] == (40, 1024)
        for tick in range(200, 250, 5):
            pair.step(tick, ele=300)
        for tick in range(250, 280, 5):
            pair.step(tick)
        assert port.output[:2] == (0, 0) and port.output[4:] == (0, 0)
        pair.step(280, sc=0)
        pair.step(285, sc=0)
        for tick in range(290, 340, 5):
            pair.step(tick)
        assert port.output[:2] == (75, 1024) and port.output[4:] == (40, 1024)


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


def test_real_delayed_ticket_does_not_refresh_distance_output():
    with actual_radio() as port:
        pair = Pair(port)
        pair.arm()
        for tick in range(70, 90, 5):
            pair.step(tick)
        delayed = next(packet for packet in reversed(port.writes) if packet.startswith(b"DS1 "))
        port.pending.clear()
        output = port.callback(130, packet=delayed)
        assert output[:2] == (0, 0) and output[4:] == (0, 0)
        assert b" T E D\n" in port.received
