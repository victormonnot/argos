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
        lines = packet.decode().split("\n")[:-1]
        if lines[0] == "#":
            assert len(lines) == 2 and lines[1].startswith("ARGOS_VISION_BEGIN ")
            lines = lines[1:]
        assert len(lines) == 1
        parts = lines[0].split()
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
    """Periodic analyses with separate camera-receipt and completion times."""

    def __init__(self, clock, http_delay, *, completion_age=.1, result_period=.2):
        self.clock = clock
        self.http_delay = http_delay
        self.completion_age = completion_age
        self.result_period = result_period
        self.validator = PreviewValidator()
        self.values = []
        self.read_times = []
        self.freeze_after = None
        self.fail_at = None

    def read(self):
        started = self.clock.now
        self.read_times.append(started)
        if len(self.read_times) == self.fail_at:
            raise bridge.PreviewError("selection cleared while awaiting a fresh image")
        self.clock.sleep(self.http_delay / 2)
        # The console clock has a different origin from the helper's clock.
        at = self.clock.now - 90
        publication_at = (min(at, self.freeze_after)
                          if self.freeze_after is not None else at)
        frame = math.floor((publication_at - self.completion_age) / self.result_period)
        received = frame * self.result_period
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
                       "age_limit_s": 1., "inference_ms": self.completion_age * 1000},
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
@pytest.mark.parametrize("http_delay", [.001, .003, .005])
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
    # Some read-only polls may await enough lifetime without emitting a SET.
    assert len(source.values) >= len(radio.values) + 1
    for sent, expired, value, ttl in radio.values:
        proposal = next(proposal for started, proposal in
                        reversed(list(zip(source.read_times, source.values)))
                        if started <= sent)
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
    assert_guarded_commands(source, radio)


def assert_guarded_commands(source, radio):
    """Check emitted SETs against the actual last validated source read."""
    for sent, expired, value, ttl in radio.values:
        proposal = next(proposal for started, proposal in
                        reversed(list(zip(source.read_times, source.values)))
                        if started <= sent)
        assert 14 <= ttl <= 20
        assert value == proposal.value
        assert sent < expired <= proposal.deadline
        assert expired <= proposal.received_at + 90 + .45
    assert all(later[0] - earlier[0] >= .1 - 1e-9
               for earlier, later in zip(radio.values, radio.values[1:]))
    # Every subsequent write retains the full allowed duration before the
    # previous value's independently tick-quantized expiry at the radio.
    assert all(later[0] + bridge.WRITE_TIMEOUT < earlier[1]
               for earlier, later in zip(radio.values, radio.values[1:]))


@pytest.mark.parametrize("phase", [.001, .03, .07, .11, .124])
@pytest.mark.parametrize("completion_age", [.08, .12, .15, .18])
@pytest.mark.parametrize("http_delay", [.003, .008, .015])
def test_eight_hz_analyses_complete_with_camera_age_and_bounded_http_delay(
        phase, completion_age, http_delay, capsys):
    # Four-thread worker measurements motivate this candidate, but completion
    # age also includes the frame's age when submitted. Exercise 80--180 ms
    # publication age independently from the candidate's 125 ms result period.
    clock = Clock(phase)
    source = DetectorSource(clock, http_delay, completion_age=completion_age,
                            result_period=.125)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    bridge.run_bridge(source, bench, 20)

    assert bench.finished and not bench.failed
    assert radio.expirations == 1 and not radio.active
    assert 150 <= len(radio.values) <= 200
    assert "Lua reports expiry" in capsys.readouterr().out
    assert_guarded_commands(source, radio)


@pytest.mark.parametrize("phase", [.001, .03, .07, .11, .124])
def test_eight_hz_candidate_tolerates_150ms_results_with_short_http_reads(phase):
    # The requested rate is an upper bound: a slower worker may deliver fewer
    # than eight results per second. This particular modeled margin still fits.
    clock = Clock(phase)
    source = DetectorSource(clock, .008, completion_age=.18, result_period=.15)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    bridge.run_bridge(source, bench, 20)

    assert bench.finished and not bench.failed
    assert radio.expirations == 1 and not radio.active
    assert 130 <= len(radio.values) <= 200
    assert_guarded_commands(source, radio)


@pytest.mark.parametrize("phase", [.001, .03, .07, .11, .124])
def test_eight_hz_candidate_is_not_a_guarantee_when_results_and_http_are_slower(phase):
    # Faster requested analysis cannot make every sampling alignment viable.
    # With 150 ms publications and 15 ms reads, some SETs use the minimum TTL;
    # the next read can then spend the previous output's delivery reserve.
    clock = Clock(phase)
    source = DetectorSource(clock, .015, completion_age=.12, result_period=.15)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    with pytest.raises(bridge.ProbeError, match="command deadline"):
        bridge.run_bridge(source, bench, 20)

    assert bench.failed and not bench.finished
    assert 1 <= len(radio.values) <= 20
    assert clock.now < radio.values[-1][1]
    assert_guarded_commands(source, radio)
    count = len(radio.values)
    clock.sleep(.3)
    radio.expire()
    assert not radio.active and radio.expirations == 1
    with pytest.raises(bridge.ProbeError):
        bench.connect()
    assert len(radio.values) == count


def test_initial_40ms_ttl_waits_for_next_analysis_without_emitting_short_set():
    # First post-handshake snapshot is valid for ~74ms: the old host sent a
    # 40 ms TTL, then waited 100 ms and encountered an already-expired session.
    clock = Clock(.065)
    source = DetectorSource(clock, .003, completion_age=.18)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    # Only validate initial admission here. Sustaining this slow stream now
    # requires room to deliver the next SET before the old value expires.
    source.read()
    bench.connect()
    selected = bridge.fresh_proposal(source, bench, clock.now + 1.)
    bench.send(selected, min_ttl=bridge.MIN_STREAM_TTL)
    bench.finish()

    assert bench.finished and not bench.failed
    assert source.values[1].frame_sequence < source.values[2].frame_sequence
    assert radio.values[0][0] >= source.read_times[2]
    assert len(source.values) > len(radio.values) + 1
    assert_guarded_commands(source, radio)
    assert radio.expirations == 1


@pytest.mark.parametrize("phase", [.001, .05, .099, .15, .199])
def test_180ms_analysis_stops_before_spending_old_output_delivery_reserve(phase):
    clock = Clock(phase)
    source = DetectorSource(clock, .003, completion_age=.18)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    # The previous success expectation omitted the 30 ms old-output transfer
    # reserve. A 200 ms result period cannot rely on its last 30 ms to send.
    with pytest.raises(bridge.ProbeError, match="command deadline"):
        bridge.run_bridge(source, bench, 1)

    assert bench.failed and not bench.finished
    assert 1 <= len(radio.values) <= 3
    assert clock.now < radio.values[-1][1]
    assert_guarded_commands(source, radio)
    with pytest.raises(bridge.ProbeError):
        bench.connect()


def test_new_analysis_cannot_arrive_during_old_radio_expiry_write_reserve():
    # With a 5 ms GET, the old host attempted SET2 at 100.389: its modeled
    # expiry was 100.394, but the radio tick expired at 100.390 and the 4 ms
    # serial write arrived at 100.393. Fresh image data cannot restore a radio
    # session that expires in transit. This must stop before sending SET2.
    clock = Clock(.05)
    source = DetectorSource(clock, .005, completion_age=.18)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    with pytest.raises(bridge.ProbeError, match="command deadline"):
        bridge.run_bridge(source, bench, 2)

    assert bench.failed and len(radio.values) == 1
    assert clock.now < radio.values[0][1]
    assert_guarded_commands(source, radio)
    clock.sleep(.1)
    radio.expire()
    assert not radio.active and radio.expirations == 1
    with pytest.raises(bridge.ProbeError):
        bench.connect()


@pytest.mark.parametrize("phase", [.001, .05, .099, .15, .199])
def test_slower_analysis_remains_bounded_across_initial_sampling_phases(phase):
    # 180 ms worker + 30 ms camera receipt age means 210 ms at publication.
    # At a 5 Hz result cadence, this is too close to the 450 ms source fence to
    # promise an uninterrupted run with 200 ms max TTL and 100 ms send spacing.
    clock = Clock(phase)
    source = DetectorSource(clock, .003, completion_age=.21)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    try:
        bridge.run_bridge(source, bench, 1)
    except (bridge.ProbeError, bridge.PreviewError):
        assert bench.failed
        command_count = len(radio.values)
        with pytest.raises(bridge.ProbeError):
            bench.connect()
        assert len(radio.values) == command_count
    else:
        assert bench.finished
    assert clock.now < 102.5
    assert_guarded_commands(source, radio)


def test_no_new_analysis_expires_without_short_commands_or_session_restart():
    clock = Clock(.065)
    source = DetectorSource(clock, .003, completion_age=.18)
    source.freeze_after = clock.now - 90
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    with pytest.raises((bridge.ProbeError, bridge.PreviewError)):
        bridge.run_bridge(source, bench, 1)

    assert bench.failed
    assert radio.values == []
    assert clock.now < 101.2
    assert len(source.values) >= 3
    with pytest.raises(bridge.ProbeError):
        bench.connect()


def test_missing_next_analysis_stops_active_session_without_expiry_revival():
    clock = Clock(.1)
    source = DetectorSource(clock, .003, completion_age=.18)
    source.freeze_after = clock.now - 90
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    with pytest.raises((bridge.ProbeError, bridge.PreviewError)):
        bridge.run_bridge(source, bench, 1)

    assert bench.failed
    assert len(radio.values) == 1
    assert len(source.values) > 3
    assert clock.now <= radio.values[0][0] + .25
    assert_guarded_commands(source, radio)
    with pytest.raises(bridge.ProbeError):
        bench.connect()
    assert len(radio.values) == 1


def test_valid_preview_with_insufficient_continuous_output_margin_stops():
    # Preview stays below 450 ms, but 240 ms completion age + 200 ms cadence
    # leaves only 10 ms near publication. USB reserve makes continuous output
    # infeasible; the expected behavior is a bounded stop, not extending TTL.
    clock = Clock(.14)
    source = DetectorSource(clock, .003, completion_age=.24)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    with pytest.raises((bridge.ProbeError, bridge.PreviewError)):
        bridge.run_bridge(source, bench, 1)

    assert bench.failed
    assert clock.now < 101.25
    assert_guarded_commands(source, radio)
    assert radio.values
    count = len(radio.values)
    with pytest.raises(bridge.ProbeError):
        bench.connect()
    assert len(radio.values) == count


def test_selection_loss_while_waiting_does_not_emit_first_command():
    clock = Clock(.065)
    source = DetectorSource(clock, .003, completion_age=.18)
    source.fail_at = 3
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    with pytest.raises(bridge.PreviewError, match="selection cleared"):
        bridge.run_bridge(source, bench, 1)

    assert bench.failed and radio.values == []
    assert len(source.read_times) == 3


def test_radio_idle_during_freshness_wait_is_terminal_even_if_preview_recovers():
    class ExpiringRadio(TickingRadio):
        injected = False

        def read(self, size):
            if (self.values and not self.injected
                    and self.clock.now >= self.values[0][0] + .115):
                self.injected = True
                self.active = False
                self.replies.append(
                    f"ARGOS_VISION_IDLE {self.session} {self.sequence}\n".encode())
            return super().read(size)

    clock = Clock(.1)
    source = DetectorSource(clock, .003, completion_age=.18)
    radio = ExpiringRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)

    with pytest.raises(bridge.ProbeError, match="ARGOS_VISION_IDLE"):
        bridge.run_bridge(source, bench, 1)

    assert radio.injected and bench.failed
    assert len(radio.values) == 1
    assert_guarded_commands(source, radio)
    clock.sleep(.5)
    with pytest.raises(bridge.ProbeError):
        bench.send(source.read())
    assert len(radio.values) == 1


def test_persistently_valid_but_insufficient_lifetime_has_bounded_initial_wait():
    # An upstream backlog could keep producing new but old images. No radio
    # output may start, and their continued arrival must not renew the 1 s wait.
    clock = Clock(.065)
    source = DetectorSource(clock, .003, completion_age=.35, result_period=.05)
    radio = TickingRadio(clock)
    bench = bridge.VisionBench(radio, clock=clock.read, sleep=clock.sleep)
    started = clock.now

    with pytest.raises(bridge.ProbeError, match="command deadline"):
        bridge.run_bridge(source, bench, 2)

    assert bench.failed and radio.values == []
    assert 1. <= clock.now - started <= 1.02
    assert len({value.frame_sequence for value in source.values}) > 2
