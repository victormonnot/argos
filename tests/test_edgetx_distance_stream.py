"""Bounded software checks for the isolated pitch experiment's wire boundary."""
from collections import deque
from dataclasses import replace

import pytest

from argos.backends import edgetx_distance_stream as stream
from argos.backends.distance_source import DistanceDemand as Demand


class Clock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += delta


class Radio:
    def __init__(self, clock):
        self.clock = clock
        self.incoming = deque([stream.HELLO + b"\n"])
        self.writes = []
        self.session = None
        self.gen = 1
        self.ticket = 0
        self.ack = 0
        self.state, self.mode = "M", "N"
        self.out_waiting = 0
        self.accept = True
        self.partial = False
        self.write_delay = 0
        self.closed = False

    @property
    def in_waiting(self):
        return sum(map(len, self.incoming))

    def read(self, maximum):
        if not self.incoming:
            return b""
        value = self.incoming.popleft()
        if len(value) > maximum:
            self.incoming.appendleft(value[maximum:])
        return value[:maximum]

    def write(self, packet):
        self.writes.append((self.clock(), packet))
        self.clock.advance(self.write_delay)
        if self.partial:
            return len(packet) - 1
        for line in packet.decode().splitlines():
            fields = line.split()
            if fields[0] == "DB3":
                self.session = fields[1]
                self.ack = 0
                self.state, self.mode = "M", "N"
                self.gen += 1
            elif fields[0] == "DS3" and self.accept:
                self.ack = int(fields[4])
        return len(packet)

    def report(self, *, state=None, mode=None, ticket=None, cause=None, ack=None, phase=None, yaw_phase=None):
        self.ticket = (self.ticket + 1) % stream.COUNTER_MODULUS if ticket is None else ticket
        self.state = state or self.state
        self.mode = mode or self.mode
        cause = cause or {"M": "M", "T": "W", "A": "A", "F": "L"}[self.state]
        phase = phase or ("A" if self.mode == "D" else "N")
        yaw_phase = yaw_phase or ("N" if self.mode == "N" else "A")
        self.incoming.append((f"DY3 {self.session} {self.gen} {self.ticket} "
                              f"{self.ack if ack is None else ack} {self.state} {cause} {self.mode} {yaw_phase} {phase}\n").encode())

    def commands(self):
        return [(at, packet.decode().split()) for at, packet in self.writes
                if packet.startswith(b"DS3 ")]

    def close(self):
        self.closed = True

    def reset_input_buffer(self):
        pass


def sample(clock, *, valid=True, mode="D", pitch=40, pitch_valid=True,
           key=("run", "camera", 8, 1), lifetime=.4, frame=None):
    return stream.SourceSample(Demand(64, valid, key, clock() + lifetime, "tracking",
                                     frame_sequence=frame, pitch=pitch,
                                     pitch_valid=pitch_valid, mode=mode), clock())


def setup(callback=None):
    clock = Clock()
    radio = Radio(clock)
    nonces = iter(f"{value:08x}" for value in range(1, 10000))
    host = stream.DistanceStream(radio, clock=clock, nonce=lambda: next(nonces),
                                 request_selection=callback)
    host.step(sample(clock))
    radio.report()
    host.step(sample(clock))
    assert not radio.commands()
    return host, radio, clock


def enable(host, radio, clock, mode="D", value=None):
    radio.gen += 1
    radio.report(state="T", mode=mode)
    host.step(value or sample(clock, mode=mode))


def tick(host, radio, clock, value=None, *, delta=.101):
    clock.advance(delta)
    radio.report()
    host.step(value or sample(clock))


@pytest.mark.parametrize("mode,expected", [("Y", ["1", "64", "0", "0"]),
                                           ("D", ["1", "64", "1", "40"])])
def test_explicit_mode_gates_pitch_without_suppressing_yaw(mode, expected):
    host, radio, clock = setup()
    enable(host, radio, clock, mode)
    assert radio.commands()[-1][1][-4:] == expected
    assert host.snapshot()["radio_mode"] == mode
    assert host.snapshot()["last_sent_pitch"] == (40 if mode == "D" else 0)


def test_mode_mismatch_withdraws_both_axes_and_does_not_renew_old_pitch():
    host, radio, clock = setup()
    enable(host, radio, clock)
    tick(host, radio, clock, sample(clock, mode="Y"))
    assert radio.commands()[-1][1][-4:] == ["0", "0", "0", "0"]
    assert not host.snapshot()["last_sent_pitch_valid"]


@pytest.mark.parametrize("kwargs", [dict(valid=False), dict(lifetime=-.1),
                                   dict(pitch_valid=False)])
def test_pitch_invalidity_and_image_deadline_release_pitch(kwargs):
    host, radio, clock = setup()
    enable(host, radio, clock)
    tick(host, radio, clock, sample(clock, **kwargs))
    fields = radio.commands()[-1][1]
    assert fields[-2:] == ["0", "0"]
    assert fields[-4:-2] == (["1", "64"] if kwargs == dict(pitch_valid=False) else ["0", "0"])


@pytest.mark.parametrize("field,value", [("pitch", 52), ("pitch", -52), ("pitch", True),
                                         ("pitch", 4.5), ("pitch_valid", 1),
                                         ("mode", "Z"), ("value", 206)])
def test_malformed_demands_fail_connection_before_transmission(field, value):
    host, radio, clock = setup()
    enable(host, radio, clock)
    old_count = len(radio.commands())
    current = sample(clock)
    with pytest.raises(stream.ProbeError, match="source demand"):
        tick(host, radio, clock, replace(current, demand=replace(current.demand, **{field: value})))
    assert host.failed and len(radio.commands()) == old_count


def test_source_mode_selection_is_bound_to_observed_generation():
    requested = []
    host, radio, clock = setup(lambda token, mode: requested.append((token, mode)))
    enable(host, radio, clock)
    token = f"{host.session}:{radio.gen}"
    assert requested == [(token, "D")]
    assert radio.commands()[-1][1][-4:] == ["0", "0", "0", "0"]
    result = stream.SelectionResult(token, clock(), ("run", "camera", 8, 1), True)
    value = replace(sample(clock), selection_result=result)
    tick(host, radio, clock, value)
    assert radio.commands()[-1][1][-4:] == ["1", "64", "1", "40"]
    assert not host.selection_pending


@pytest.mark.parametrize("axis", ["yaw", "pitch", "both"])
def test_temporary_axis_phases_keep_target_reference_and_stream(axis):
    requested = []
    host, radio, clock = setup(lambda token, mode: requested.append((token, mode)))
    enable(host, radio, clock)
    token = f"{host.session}:{radio.gen}"
    key = ("run", "camera", 8, 1)
    result = stream.SelectionResult(token, clock(), key, True)
    tick(host, radio, clock, replace(sample(clock), selection_result=result))
    original = host.session, host.status.generation, host.selection_key
    for phase in ("A", "M", "R", "A"):
        clock.advance(.101)
        radio.report(state="A", mode="D",
                     phase=phase if axis in ("pitch", "both") else "A",
                     yaw_phase=phase if axis in ("yaw", "both") else "A")
        value = sample(clock)
        value = replace(value, demand=replace(value.demand, reference_height=.45))
        host.step(value)
        assert (host.session, host.status.generation, host.selection_key) == original
        assert host.snapshot()["radio_pitch_phase"] == (phase if axis in ("pitch", "both") else "A")
        assert host.snapshot()["radio_yaw_phase"] == (phase if axis in ("yaw", "both") else "A")
        assert radio.commands()[-1][1][-4:] == ["1", "64", "1", "40"]
        assert not host.selection_pending and not host.selection_failed
    assert requested == [(token, "D")]


def test_mode_change_without_generation_is_rejected():
    host, radio, clock = setup()
    enable(host, radio, clock)
    radio.report(mode="Y")
    with pytest.raises(stream.ProbeError, match="mode changed"):
        host.step(sample(clock, mode="Y"))
    assert host.failed


def test_new_generation_in_manual_discards_old_distance_status():
    host, radio, clock = setup()
    enable(host, radio, clock)
    oldgen, oldticket = radio.gen, radio.ticket
    radio.gen += 1
    radio.report(state="M", mode="N")
    host.step(sample(clock))
    count = len(radio.commands())
    radio.incoming.append(f"DY3 {host.session} {oldgen} {oldticket + 100} 1 A A D A A\n".encode())
    clock.advance(.101)
    host.step(sample(clock))
    assert len(radio.commands()) == count and host.status.mode == "N"
    enable(host, radio, clock, "Y")
    assert radio.commands()[-1][1][-4:] == ["1", "64", "0", "0"]


def test_blocked_source_releases_both_then_rotates_and_requires_new_cycle():
    host, radio, clock = setup()
    enable(host, radio, clock)
    old = sample(clock, lifetime=.03)
    tick(host, radio, clock, old)
    assert radio.commands()[-1][1][-4:] == ["0", "0", "0", "0"]
    session = host.session
    for _ in range(12):
        tick(host, radio, clock, old)
    assert host.session != session and host.source_fault
    count = len(radio.commands())
    tick(host, radio, clock)
    assert len(radio.commands()) == count and host.status.mode == "N"


def test_changed_target_cannot_keep_old_distance_reference():
    host, radio, clock = setup()
    enable(host, radio, clock)
    session = host.session
    tick(host, radio, clock, sample(clock, key=("run", "camera", 9, 2)))
    assert host.session != session and len(radio.commands()) == 1


def test_stale_tickets_and_stuck_ack_expire_host_session():
    for duplicate in (False, True):
        host, radio, clock = setup()
        radio.accept = False
        enable(host, radio, clock)
        session, ticket = host.session, radio.ticket
        for _ in range(12):
            clock.advance(.101)
            radio.report(ticket=ticket if duplicate else None)
            host.step(sample(clock))
        assert host.session != session


def test_buffered_output_only_sends_latest_pitch_once_queue_drains():
    host, radio, clock = setup()
    enable(host, radio, clock)
    radio.out_waiting = 12
    for _ in range(4):
        tick(host, radio, clock)
    assert len(radio.commands()) == 1
    radio.out_waiting = 0
    tick(host, radio, clock, sample(clock, pitch=-12))
    assert len(radio.commands()) == 2
    assert radio.commands()[-1][1][-2:] == ["1", "-12"]


def test_command_evidence_distinguishes_sent_values_from_queued_and_invalid_pitch():
    host, radio, clock = setup()
    assert host.snapshot()["last_sent_command"] is None
    enable(host, radio, clock, value=sample(clock, frame=10))
    first = host.snapshot()["last_sent_command"]
    assert first == dict(session=host.session, generation=radio.gen, sequence=1, mode="D",
                         yaw=dict(valid=True, value=64), pitch=dict(valid=True, value=40),
                         frame_sequence=10, write_finished_at=clock())
    radio.out_waiting = 20
    tick(host, radio, clock, sample(clock, pitch=-30))
    assert host.snapshot()["last_sent_command"] == first
    radio.out_waiting = 0
    tick(host, radio, clock, sample(clock, pitch=-30, pitch_valid=False))
    sent = host.snapshot()["last_sent_command"]
    assert sent["yaw"] == dict(valid=True, value=64)
    assert sent["pitch"] == dict(valid=False, value=0)
    sent["pitch"]["value"] = 123
    assert host.last_sent_command["pitch"]["value"] == 0
    host._rotate(clock(), "test reconnect")
    assert host.snapshot()["last_sent_command"] is None


def test_partial_distance_write_is_not_published_as_a_sent_command():
    host, radio, clock = setup()
    radio.partial = True
    with pytest.raises(stream.ProbeError, match="partial"):
        enable(host, radio, clock)
    assert host.failed and host.snapshot()["last_sent_command"] is None


def test_new_frame_and_pitch_withdrawal_preserve_minimum_spacing():
    host, radio, clock = setup()
    enable(host, radio, clock, value=sample(clock, frame=1))
    tick(host, radio, clock, sample(clock, frame=2), delta=.03)
    assert len(radio.commands()) == 1
    tick(host, radio, clock, sample(clock, frame=2), delta=.021)
    assert len(radio.commands()) == 2
    tick(host, radio, clock, sample(clock, frame=2, pitch_valid=False), delta=.051)
    assert radio.commands()[-1][1][-4:] == ["1", "64", "0", "0"]
    assert len(radio.commands()) == 3


@pytest.mark.parametrize("line", [b"AY1 00000001 1 1 0 M M", b"DY3 00000001 1 1 0 A A N N N",
                                   b"DY3 00000001 1 1 0 M M D A A", b"DY3 00000001 1 1 0 T A D A A",
                                   b"DY3 00000001 2147483648 1 0 T W D A A", b"x" * 97,
                                   b"DY3 00000001 1 1 0 A A D A N",
                                   b"DY3 00000001 1 1 0 A A Y A M",
                                   b"DY3 00000001 1 1 0 A A Y N N",
                                   b"DY3 00000001 1 1 0 M M N M N",
                                   b"DY1 00000001 1 1 0 A A D",
                                   b"DY2 00000001 1 1 0 A A D A"])
def test_malformed_or_wrong_protocol_after_greeting_fails(line):
    host, radio, clock = setup()
    radio.incoming.append(line + b"\n")
    with pytest.raises(stream.ProbeError):
        host.step(sample(clock))
    assert host.failed


@pytest.mark.parametrize("greeting", [b"ARGOS_YAW_STREAM_V3", b"ARGOS_DISTANCE_STREAM_V1", b"ARGOS_DISTANCE_STREAM_V2"])
def test_old_or_yaw_peer_gets_no_writes_and_no_shared_greeting(greeting):
    clock = Clock()
    radio = Radio(clock)
    radio.incoming = deque([greeting + b"\n"])
    host = stream.DistanceStream(radio, clock=clock)
    host.step(sample(clock))
    clock.advance(5)
    with pytest.raises(stream.ProbeError, match="ARGOS_DISTANCE_STREAM"):
        host.step(sample(clock))
    assert not radio.writes


def test_maximum_fields_fit_line_limit():
    host, radio, clock = setup()
    host.status = replace(host.status, generation=stream.MAX_SEQUENCE - 1,
                          ticket=stream.MAX_SEQUENCE - 1, ack=stream.MAX_SEQUENCE - 1)
    radio.gen = radio.ticket = radio.ack = host.sent_sequence = stream.MAX_SEQUENCE - 1
    enable(host, radio, clock, value=sample(clock, pitch=-51))
    assert radio.commands()[-1][1][4] == str(stream.MAX_SEQUENCE)
    assert max(len(packet.rstrip(b"\n")) for _, packet in radio.writes) <= stream.MAX_LINE
    assert stream.Lines().feed(b"x" * 96 + b"\n") == [b"x" * 96]


def test_partial_write_fails_connection():
    host, radio, clock = setup()
    enable(host, radio, clock)
    radio.partial = True
    with pytest.raises(stream.ProbeError, match="partial"):
        tick(host, radio, clock)
    assert host.failed


def test_runner_requires_explicit_local_source_worker():
    with pytest.raises(ValueError, match="local source"):
        stream.run_stream("fake", object())


def test_runner_disconnect_reconnect_requires_fresh_physical_enable_cycle():
    clock = Clock()
    origin = clock()
    instances = []

    class Worker:
        def __init__(self, source, **kwargs):
            pass

        def start(self):
            pass

        def snapshot(self):
            return sample(clock)

        def close(self):
            pass

    class DisconnectingRadio(Radio):
        def __init__(self, first):
            super().__init__(clock)
            self.first = first
            self.last_report = clock()
            self.enabled = False

        def read(self, maximum):
            if self.first and clock() - origin > .8:
                raise OSError("unplugged")
            if self.session is not None and clock() - self.last_report >= .05:
                self.last_report = clock()
                if self.first and not self.enabled and clock() - origin > .2:
                    self.gen += 1
                    self.enabled = True
                self.report(state="T" if self.enabled else "M", mode="D" if self.enabled else "N")
            return super().read(maximum)

    def opener(device):
        radio = DisconnectingRadio(not instances)
        instances.append(radio)
        return radio

    stream.run_stream("fake", object(), duration=2.4, opener=opener, clock=clock,
                      sleep=clock.advance, report=lambda _: None, worker_factory=Worker)
    assert len(instances) == 2 and all(radio.closed for radio in instances)
    assert len(instances[0].commands()) > 1 and not instances[1].commands()
    assert instances[0].session != instances[1].session
