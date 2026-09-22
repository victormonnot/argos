"""Compose source-age validation and serial pacing across detector phases.

Synthetic clocks include bounded HTTP/USB time and an independently expiring
radio. These checks establish no physical delivery or scheduling guarantee.
"""
from collections import deque
import math

import pytest

from argos.backends import edgetx_vision_bench as bridge
from argos.backends.vision_bench_source import PreviewValidator


class Clock:
    def __init__(self, phase):
        self.now = 100.1 + phase

    def read(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class TickingRadio:
    """Receipt-clock expiry is checked before processing every input command."""

    def __init__(self, clock):
        self.clock = clock
        self.replies = deque()
        self.active = False
        self.expiry_tick = None
        self.session_tick = None
        self.session = None
        self.sequence = 0
        self.values = []
        self.expirations = 0

    def tick(self):
        return math.floor((self.clock.now + 1e-9) * 100)

    def expire(self):
        if self.active and (self.tick() >= self.session_tick + 3000
                            or (self.expiry_tick is not None
                                and self.tick() >= self.expiry_tick)):
            self.active = False
            self.expirations += 1
            self.replies.append(
                f"ARGOS_VISION_IDLE {self.session} {self.sequence}\n".encode())

    def reset_input_buffer(self):
        self.replies.clear()
        self.replies.append(bridge.HELLO + b"\n")

    def read(self, size):
        self.expire()
        return self.replies.popleft() if self.replies else b""

    def write(self, packet):
        sent = self.clock.now
        self.clock.sleep(.004)
        self.expire()
        parts = packet.decode().split()
        if parts[0] == "ARGOS_VISION_BEGIN":
            self.session = parts[1]
            self.active = True
            self.session_tick = self.tick()
            self.replies.append(f"ARGOS_VISION_READY {self.session}\n".encode())
        else:
            assert parts[0] == "ARGOS_VISION_SET"
            assert self.active, "SET arrived after radio expiry"
            _, session, sequence, value, ttl = parts
            assert session == self.session
            assert int(sequence) > self.sequence
            self.sequence = int(sequence)
            self.expiry_tick = self.tick() + int(ttl)
            self.values.append((sent, self.expiry_tick / 100, int(value), int(ttl)))
            self.replies.append(
                f"ARGOS_VISION_ACK {session} {sequence} {value} {ttl}\n".encode())
        return len(packet)


class DetectorSource:
    """A 5 Hz detector completes each frame 100 ms after its camera receipt."""

    def __init__(self, clock, http_delay):
        self.clock = clock
        self.http_delay = http_delay
        self.validator = PreviewValidator()
        self.values = []

    def read(self):
        started = self.clock.now
        self.clock.sleep(self.http_delay / 2)
        # The console clock has a different origin from the helper's clock.
        at = self.clock.now - 90
        frame = math.floor((at - .1) / .2)
        received = frame * .2
        error = (-.5, 0., .5)[frame % 3]
        snapshot = {
            "schema_version": 1, "at": at, "run_id": "run", "environment": "real",
            "reconnecting": None,
            "configuration": {"environment": "real", "video_source": "device",
                              "video_endpoint": "/dev/video0"},
            "video": {"source_id": "camera", "source": "device", "state": "recent",
                      "endpoint": "/dev/video0", "received_at": at - .005,
                      "rx_age_s": .005, "age_limit_s": 1.},
            "vision": {"configured": True, "state": "recent", "frame_age_s": at - received,
                       "age_limit_s": 1., "inference_ms": 100.},
            "yaw_preview": {"enabled": True, "phase": "tracking", "revision": 1,
                            "target_id": 7, "run_id": "run", "video_id": "camera",
                            "frame_sequence": frame + 1, "frame_received_at": received,
                            "frame_age_s": at - received, "frame_max_age_s": .45,
                            "error_x": error, "yaw": error / 4, "yaw_limit": .125},
        }
        self.clock.sleep(self.http_delay / 2)
        proposal = self.validator.validate(snapshot, started, self.clock.now)
        self.values.append(proposal)
        return proposal


@pytest.mark.parametrize("phase", [.001, .05, .099, .15, .199])
@pytest.mark.parametrize("http_delay", [.001, .003])
def test_five_hz_detector_with_100ms_inference_completes_at_each_sampling_phase(
        phase, http_delay, capsys):
    clock = Clock(phase)
    source = DetectorSource(clock, http_delay)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    bridge.run_bridge(source, bench, 20)

    assert bench.finished and not bench.failed
    assert radio.expirations == 1 and not radio.active
    assert 185 <= len(radio.values) <= 200
    assert {value for _, _, value, _ in radio.values} == {-128, 0, 128}
    assert "Lua reports expiry" in capsys.readouterr().out
    # One additional read validates the selection before the USB handshake.
    assert len(source.values) == len(radio.values) + 1
    for proposal, (sent, expired, value, ttl) in zip(source.values[1:], radio.values):
        assert value == proposal.value
        assert 1 <= ttl <= 20
        assert sent < expired <= proposal.deadline
        assert expired <= proposal.received_at + 90 + .45
    assert all(later[0] - earlier[0] >= .1 - 1e-9
               for earlier, later in zip(radio.values, radio.values[1:]))
    # Polling repeats some analyses, but cannot move their deadline forward.
    repeated = [(first, second) for first, second in zip(source.values, source.values[1:])
                if first.frame_sequence == second.frame_sequence]
    assert repeated
    assert all(second.deadline <= first.deadline for first, second in repeated)
