"""Forward delivery is independent of a fast host write and the ACK return path.

These synthetic timings do not estimate physical USB or EdgeTX scheduling.
Complete lines arrive at modeled callbacks, with a separate 10 ms receiver clock;
parser fragmentation, scheduler stalls and native mixer execution require
separate evidence.
"""
from collections import deque
from dataclasses import dataclass
import heapq
import math

import pytest

from argos.backends import edgetx_vision_bench as bridge
from argos.backends.vision_bench_source import PreviewValue
from test_vision_bench_cadence import Clock, DetectorSource


@dataclass
class Accepted:
    sequence: int
    sent_at: float
    received_at: float
    expires_at: float
    ttl: int


class QueuedRadio:
    """Event-driven radio whose receive clock never inherits host write time."""

    def __init__(self, clock, *, forward_delay, return_delay, callback_period=.01):
        self.clock = clock
        self.forward_delay = forward_delay
        self.return_delay = return_delay
        self.callback_period = callback_period
        self.events = []
        self.event_count = 0
        self.received = deque()
        self.writes = []
        self.accepted = []
        self.rejected = []
        self.expirations = []
        self.session = None
        self.sequence = 0
        self.active = False
        self.expires_at = None

    def queue(self, at, kind, payload):
        self.event_count += 1
        heapq.heappush(self.events, (at, self.event_count, kind, payload))

    def reply(self, at, line):
        self.queue(at + self.return_delay, "reply", (line + "\n").encode())

    def callback_at(self, at):
        return math.ceil((at - 1e-9) / self.callback_period) * self.callback_period

    def advance(self):
        # Process time in order even if an HTTP read advanced the host clock
        # across several events. Expiry wins ties with a queued SET callback.
        while self.events or (self.active and self.expires_at <= self.clock.now):
            event_at = self.events[0][0] if self.events else math.inf
            if self.active and self.expires_at <= min(event_at, self.clock.now):
                self.active = False
                self.expirations.append((self.expires_at, self.sequence))
                self.reply(self.expires_at,
                           f"ARGOS_VISION_IDLE {self.session} {self.sequence}")
                continue
            if event_at > self.clock.now:
                break
            at, _, kind, payload = heapq.heappop(self.events)
            if kind == "reply":
                self.received.append(payload)
                continue
            sent_at, parts = payload
            if parts[0] == "ARGOS_VISION_BEGIN":
                assert self.session is None, "the fixture must not restart a session"
                self.session = parts[1]
                self.active = True
                self.session_end = math.floor((at + 1e-9) * 100) / 100 + 30.
                self.expires_at = self.callback_at(self.session_end)
                self.reply(at, f"ARGOS_VISION_READY {self.session}")
            else:
                _, session, sequence, value, ttl = parts
                sequence, ttl = int(sequence), int(ttl)
                if not self.active:
                    self.rejected.append(sequence)
                    continue
                assert session == self.session and sequence > self.sequence
                self.sequence = sequence
                received_tick = math.floor((at + 1e-9) * 100) / 100
                self.expires_at = self.callback_at(
                    min(self.session_end, received_tick + ttl * .01))
                self.accepted.append(Accepted(sequence, sent_at, at, self.expires_at, ttl))
                self.reply(at, f"ARGOS_VISION_ACK {session} {sequence} {value} {ttl}")

    def reset_input_buffer(self):
        self.advance()
        self.received.clear()
        self.received.append(bridge.HELLO + b"\n")

    def read(self, size):
        self.advance()
        if not self.received:
            return b""
        data = self.received.popleft()
        assert len(data) <= size
        return data

    def write(self, packet):
        self.advance()
        sent_at = self.clock.now
        parts = packet.decode().strip().split("\n")[-1].split()
        sequence = int(parts[2]) if parts[0] == "ARGOS_VISION_SET" else 0
        arrival = sent_at + self.forward_delay(sequence)
        # Callback cadence and the getTime-style 10 ms clock are independent.
        # This ideal periodic model does not cover real scheduler delays.
        callback_at = self.callback_at(arrival)
        self.queue(callback_at, "input", (sent_at, parts))
        self.writes.append((sent_at, packet))
        return len(packet)  # Kernel acceptance says nothing about Lua receipt.


def setup(phase, forward_delay, return_delay, *, result_period=.1, callback_period=.01):
    clock = Clock(phase)
    source = DetectorSource(clock, .003, completion_age=.12,
                            result_period=result_period)
    radio = QueuedRadio(clock, forward_delay=forward_delay, return_delay=return_delay,
                        callback_period=callback_period)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)
    return clock, source, radio, bench


@pytest.mark.parametrize("phase", [.001, .037, .079])
@pytest.mark.parametrize("return_delay", [.004, .025])
def test_bounded_variable_forward_delivery_completes_and_expires(phase, return_delay):
    _, source, radio, bench = setup(
        phase, lambda sequence: (.004, .012, .018)[sequence % 3], return_delay)

    bridge.run_bridge(source, bench, 20, new_frames_only=True)

    assert bench.finished and not bench.failed
    assert not radio.active and not radio.rejected
    assert 150 < len(radio.accepted) <= 200
    assert radio.expirations == [(radio.accepted[-1].expires_at, len(radio.accepted))]
    assert len(radio.writes) == len(radio.accepted) + 1  # One BEGIN, then SETs.
    assert all(0 < row.received_at - row.sent_at < .03 for row in radio.accepted)
    assert all(after.received_at < before.expires_at
               for before, after in zip(radio.accepted, radio.accepted[1:]))
    assert {value.target_id for value in source.values} == {7}


def test_fresh_source_and_fast_write_can_deliver_set_after_previous_radio_expiry():
    clock, source, radio, bench = setup(
        .001, lambda sequence: .09 if sequence == 4 else .004, .005,
        result_period=.15)

    with pytest.raises(bridge.ProbeError, match="unexpected.*ARGOS_VISION_IDLE"):
        bridge.run_bridge(source, bench, 20, new_frames_only=True)

    assert bench.failed and not bench.finished
    assert [row.sequence for row in radio.accepted] == [1, 2, 3]
    assert radio.expirations == [(radio.accepted[-1].expires_at, 3)]
    sent_at, packet = radio.writes[-1]
    assert packet.startswith(f"ARGOS_VISION_SET {bench.session} 4 ".encode())
    assert source.values[-1].deadline - sent_at > .23
    # Host-side send and freshness guards passed: an immediate write return
    # cannot guarantee the modeled receiver will see the packet before expiry.
    assert sent_at < radio.accepted[-1].sent_at + .2 - .03
    assert bench.last_sent == sent_at
    count = len(radio.writes)
    clock.sleep(.5)
    radio.advance()
    assert radio.rejected == [4]
    with pytest.raises(bridge.ProbeError):
        bench.send(source.values[-1])
    with pytest.raises(bridge.ProbeError):
        bench.connect()
    assert len(radio.writes) == count
    assert sum(b"ARGOS_VISION_BEGIN" in packet for _, packet in radio.writes) == 1


def test_delayed_receipt_ttl_is_not_an_original_image_expiry_guarantee():
    # Document the limit of a receipt-relative TTL: even a timely ACK does not
    # prove the output stopped before the image's original host-side deadline.
    clock, _, radio, bench = setup(.001, lambda sequence: .08 if sequence else .004, .005)
    bench.connect()
    proposal = PreviewValue(40, clock.now + .24, 1, 7, 1, "run", "camera", 1., 100.)

    sequence, ttl = bench.send(proposal, min_ttl=20)

    accepted = radio.accepted[0]
    assert (sequence, ttl) == (1, 20)
    assert not bench.failed
    assert bench.last_expiry < proposal.deadline < accepted.expires_at
    assert accepted.received_at - accepted.sent_at >= .08
    bench.finish()
    assert bench.finished and not radio.active
    assert radio.expirations == [(accepted.expires_at, 1)]


def test_50ms_callbacks_can_expire_with_151_5ms_analyses_and_no_transit_delay():
    # A nominal 50 ms mixer callback is distinct from getTime's 10 ms units.
    # Drifting publication phase can put consecutive writes four callbacks
    # apart: the receiver checks its 200 ms expiry before reading the next SET.
    clock, source, radio, bench = setup(
        .001, lambda sequence: 0., .016, result_period=.1515, callback_period=.05)

    with pytest.raises(bridge.ProbeError, match="unexpected.*ARGOS_VISION_IDLE"):
        bridge.run_bridge(source, bench, 20, new_frames_only=True)

    assert bench.failed and not bench.finished
    previous = radio.accepted[-1]
    sent_at, _ = radio.writes[-1]
    assert previous.ttl == 20
    assert .15 < sent_at - previous.sent_at < .17
    assert sent_at < previous.sent_at + .2 - .03
    assert source.values[-1].deadline - sent_at > .23
    assert {value.target_id for value in source.values} == {7}
    assert radio.callback_at(sent_at) == pytest.approx(previous.received_at + .2)
    assert radio.expirations == [(previous.expires_at, previous.sequence)]
    clock.sleep(.1)
    radio.advance()
    assert radio.rejected == [previous.sequence + 1]
    count = len(radio.writes)
    with pytest.raises(bridge.ProbeError):
        bench.send(source.values[-1])
    with pytest.raises(bridge.ProbeError):
        bench.connect()
    assert len(radio.writes) == count


@pytest.mark.parametrize("result_period", [.1, .15])
def test_50ms_callbacks_can_complete_with_constant_aligned_cadence(result_period):
    _, source, radio, bench = setup(
        .001, lambda sequence: 0., .016, result_period=result_period, callback_period=.05)

    bridge.run_bridge(source, bench, 20, new_frames_only=True)

    assert bench.finished and not bench.failed
    assert not radio.rejected and not radio.active
    assert len(radio.accepted) >= 130
    assert all(after.received_at < before.expires_at
               for before, after in zip(radio.accepted, radio.accepted[1:]))
    assert radio.expirations == [(radio.accepted[-1].expires_at, len(radio.accepted))]
