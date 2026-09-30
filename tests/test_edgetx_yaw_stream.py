"""Targeted continuous transport tests, with bounded queues and virtual time."""
from collections import deque
from dataclasses import replace
import threading
import time

import pytest

from argos.backends import edgetx_yaw_stream as stream
from argos.backends.yaw_stream_source import YawDemand


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
        self.state = "M"
        self.out_waiting = 0
        self.partial = False
        self.write_delay = 0
        self.accept = True

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
            if fields[0] == "AB1":
                self.session = fields[1]
                self.ack = 0
                self.state = "M"
                self.gen += 1
            elif fields[0] == "AS1" and self.accept:
                self.ack = int(fields[4])
        return len(packet)

    def report(self, *, ticket=None, state=None, ack=None, cause=None):
        self.ticket = self.ticket + 1 if ticket is None else ticket
        if state is not None:
            self.state = state
        cause = cause or {"M": "M", "T": "W", "A": "A", "F": "L"}[self.state]
        self.incoming.append((f"AY1 {self.session} {self.gen} {self.ticket} "
                              f"{self.ack if ack is None else ack} {self.state} {cause}\n").encode())

    def commands(self):
        return [(at, packet.decode().split()) for at, packet in self.writes if packet.startswith(b"AS1 ")]

    def begins(self):
        return [packet for _, packet in self.writes if b"AB1 " in packet]


def sample(clock, *, valid=True, value=64, key=("run", "camera", 8, 1), lifetime=.4):
    return stream.SourceSample(YawDemand(value, valid, key, clock() + lifetime, "tracking"), clock())


def setup():
    clock = Clock()
    radio = Radio(clock)
    nonces = iter(f"{value:08x}" for value in range(1, 10000))
    host = stream.YawStream(radio, clock=clock, nonce=lambda: next(nonces))
    host.step(sample(clock))
    radio.report(state="M")
    host.step(sample(clock))
    assert radio.begins() and not radio.commands()
    return host, radio, clock


def arm(host, radio, clock):
    radio.gen += 1  # an explicit operator SC middle -> up cycle
    radio.report(state="T")
    host.step(sample(clock))
    return radio.commands()[-1]


def tick(host, radio, clock, *, advance=.1, value=None, report=True):
    clock.advance(advance)
    if report:
        radio.report()
    host.step(sample(clock) if value is None else value)


def test_manual_greeting_and_explicit_radio_arm_precede_any_commands():
    host, radio, clock = setup()
    for _ in range(15):
        tick(host, radio, clock)
    assert not radio.commands()  # zero acknowledgements in manual are healthy
    at, fields = arm(host, radio, clock)
    assert fields[-2:] == ["1", "64"]
    assert fields[2] == str(radio.gen)
    assert fields[3] == str(radio.ticket)
    assert radio.timeout == 0 and radio.write_timeout == .02


def test_continues_for_minutes_without_session_or_command_count_limit():
    host, radio, clock = setup()
    arm(host, radio, clock)
    session = host.session
    for _ in range(2400):
        tick(host, radio, clock, advance=.101)
    commands = radio.commands()
    assert len(commands) == 2401
    assert host.session == session and not host.failed
    assert all(later[0] - earlier[0] >= .1 - 1e-9 for earlier, later in zip(commands, commands[1:]))


def test_paused_target_sends_invalid_not_a_valid_zero_and_recovers_same_session():
    host, radio, clock = setup()
    arm(host, radio, clock)
    session = host.session
    for _ in range(4):
        clock.advance(.101)
        radio.report()
        host.step(sample(clock, valid=False, value=0))
        assert radio.commands()[-1][1][-2:] == ["0", "0"]
    tick(host, radio, clock, advance=.101)
    assert radio.commands()[-1][1][-2:] == ["1", "64"]
    assert host.session == session
    clock.advance(.101)
    radio.report()
    host.step(sample(clock, value=0))
    assert radio.commands()[-1][1][-2:] == ["1", "0"]  # centered is distinct


def test_expired_mailbox_image_is_never_renewed_even_if_source_worker_is_blocked():
    host, radio, clock = setup()
    arm(host, radio, clock)
    old = sample(clock, lifetime=.05)
    tick(host, radio, clock, advance=.101, value=old)
    assert radio.commands()[-1][1][-2:] == ["0", "0"]
    session = host.session
    for _ in range(10):
        tick(host, radio, clock, advance=.101, value=old)
    assert host.session != session and host.source_fault
    assert not host.failed
    # Recovery cannot send against the previous T status or old nonce.
    count = len(radio.commands())
    tick(host, radio, clock, advance=.101)
    assert len(radio.commands()) == count
    assert host.status.state == "M"


def test_target_reselection_rotates_session_and_requires_new_operator_cycle():
    host, radio, clock = setup()
    arm(host, radio, clock)
    old_session = host.session
    clock.advance(.101)
    radio.report()
    new = sample(clock, key=("run", "camera", 9, 2))
    host.step(new)
    assert host.session != old_session and len(radio.commands()) == 1
    radio.report(state="M")
    host.step(new)
    assert len(radio.commands()) == 1


def test_missing_short_return_reports_does_not_create_stop_and_wait():
    host, radio, clock = setup()
    arm(host, radio, clock)
    session = host.session
    for _ in range(4):
        tick(host, radio, clock, advance=.101, report=False)
    assert len(radio.commands()) == 5  # no intervening ACKs
    tick(host, radio, clock, advance=.101)
    assert host.session == session and host.status.ack == 5


def test_advancing_status_with_stuck_ack_requires_rearm():
    host, radio, clock = setup()
    radio.accept = False
    arm(host, radio, clock)
    session = host.session
    for _ in range(11):
        tick(host, radio, clock, advance=.101)
    assert host.session != session
    assert "AB1" in radio.writes[-1][1].decode()
    assert not host.failed


def test_duplicate_tickets_do_not_keep_return_path_healthy():
    host, radio, clock = setup()
    arm(host, radio, clock)
    session, ticket = host.session, radio.ticket
    for _ in range(11):
        clock.advance(.101)
        radio.report(ticket=ticket)
        host.step(sample(clock))
    assert host.session != session


def test_generation_change_discards_old_ticket_and_does_not_reuse_output_after_manual():
    host, radio, clock = setup()
    arm(host, radio, clock)
    oldgen, oldticket = radio.gen, radio.ticket
    clock.advance(.101)
    radio.gen += 1
    radio.report(state="M")
    host.step(sample(clock))
    count = len(radio.commands())
    radio.incoming.append(f"AY1 {host.session} {oldgen} {oldticket + 100} 1 A A\n".encode())
    clock.advance(.101)
    host.step(sample(clock))
    assert len(radio.commands()) == count and host.status.state == "M"


def test_no_catchup_burst_after_scheduler_gap():
    host, radio, clock = setup()
    arm(host, radio, clock)
    clock.advance(.45)
    radio.report()
    host.step(sample(clock))
    count = len(radio.commands())
    for _ in range(20):
        host.step(sample(clock))
    assert len(radio.commands()) == count


def test_output_backpressure_retains_only_latest_demand_and_never_queues_commands():
    host, radio, clock = setup()
    arm(host, radio, clock)
    radio.out_waiting = 12
    for _ in range(5):
        tick(host, radio, clock, advance=.101)
    assert len(radio.commands()) == 1
    radio.out_waiting = 0
    clock.advance(.101)
    radio.report()
    host.step(sample(clock, value=-31))
    assert radio.commands()[-1][1][-1] == "-31"
    assert len(radio.commands()) == 2


def test_stalled_output_and_partial_write_end_this_connection():
    host, radio, clock = setup()
    arm(host, radio, clock)
    radio.partial = True
    with pytest.raises(stream.ProbeError, match="partial"):
        tick(host, radio, clock, advance=.101)
    assert host.failed
    count = len(radio.writes)
    with pytest.raises(stream.ProbeError, match="closed"):
        host.step(sample(clock))
    assert len(radio.writes) == count


@pytest.mark.parametrize("line", [b"AY1 ab 1 2 3 A A", b"AY1 00000001 2147483648 1 0 M M",
                                   b"AY1 00000001 1 2147483648 0 M M",
                                   b"AY1 00000001 1 2 2147483648 M M", b"x" * 65,
                                   b"AY1 00000001 1 2 0 X A", b"AY1 00000001 1 2 0 M",
                                   b"AY1 00000001 1 2 0 T Z", b"AY1 00000001 1 2 0 T A",
                                   b"CLI prompt"])
def test_invalid_radio_framing_fails_connection(line):
    host, radio, clock = setup()
    radio.incoming.append(line + b"\n")
    with pytest.raises(stream.ProbeError):
        host.step(sample(clock))
    assert host.failed


def test_wrong_peer_gets_no_bytes_and_timeout_is_bounded():
    clock = Clock()
    radio = Radio(clock)
    radio.incoming = deque([b"CLI\n"])
    host = stream.YawStream(radio, clock=clock)
    host.step(sample(clock))
    clock.advance(5)
    with pytest.raises(stream.ProbeError, match="greeting|ARGOS"):
        host.step(sample(clock))
    assert not radio.writes


def test_long_source_error_rotates_once_during_outage_not_each_tick():
    host, radio, clock = setup()
    arm(host, radio, clock)
    before = len(radio.begins())
    for _ in range(30):
        clock.advance(.101)
        radio.report()
        host.step(stream.SourceSample(None, clock(), True))
    assert len(radio.begins()) == before + 1
    assert host.source_fault and host.status.state == "M"


def test_reader_backlog_is_bounded_and_no_transmission_uses_intermediate_ticket():
    host, radio, clock = setup()
    arm(host, radio, clock)
    for _ in range(80):
        radio.report()
    clock.advance(.101)
    before = len(radio.commands())
    host.step(sample(clock))
    assert radio.in_waiting > 0 and len(radio.commands()) == before
    while radio.in_waiting:
        host.step(sample(clock))
    assert radio.commands()[-1][1][3] == str(radio.ticket)


def test_source_worker_blocked_read_does_not_block_mailbox_or_serial_shutdown():
    entered, release = threading.Event(), threading.Event()

    class BlockedSource:
        closed = False

        def read(self):
            entered.set()
            release.wait(2)
            return YawDemand(40, True, ("person",), time.monotonic() + .4, "tracking")

        def close(self):
            self.closed = True

    source = BlockedSource()
    worker = stream.SourceWorker(source)
    worker.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        assert worker.snapshot().error
        worker.close()
        assert time.monotonic() - started < .5
    finally:
        release.set()
        worker._thread.join(timeout=1)
    assert source.closed


def test_status_ticket_counter_wrap_preserves_progress():
    host, radio, clock = setup()
    host.status = replace(host.status, ticket=2**31 - 1)
    radio.report(ticket=0)
    host.step(sample(clock))
    assert host.status.ticket == 0


def test_cli_loop_reconnects_to_fresh_nonce_and_stays_manual(monkeypatch):
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
            self.closed = False

        def reset_input_buffer(self):
            pass

        def close(self):
            self.closed = True

        def read(self, maximum):
            if self.first and clock() - origin > .8:
                raise OSError("unplugged")
            if self.session is not None and clock() - self.last_report >= .05:
                self.last_report = clock()
                # Only first connection has an explicit operator arm event.
                self.report(state="T" if self.first and clock() - origin > .2 else "M")
            return super().read(maximum)

    def open_again(device):
        radio = DisconnectingRadio(not instances)
        instances.append(radio)
        return radio

    monkeypatch.setattr(stream, "SourceWorker", Worker)
    messages = []
    stream.run_stream("fake-pocket", object(), duration=2.4, opener=open_again,
                      clock=clock, sleep=clock.advance, report=messages.append)
    assert len(instances) == 2 and all(radio.closed for radio in instances)
    assert len(instances[0].commands()) > 1 and not instances[1].commands()
    assert instances[0].session != instances[1].session
    assert any("waiting to reconnect" in message for message in messages)


@pytest.mark.parametrize("bad_ack", [0, 20])
def test_ack_regression_or_unsent_sequence_is_rejected(bad_ack):
    host, radio, clock = setup()
    arm(host, radio, clock)
    tick(host, radio, clock, advance=.101)
    assert host.status.ack == 1
    clock.advance(.101)
    radio.report(ack=bad_ack)
    with pytest.raises(stream.ProbeError, match="acknowledge"):
        host.step(sample(clock))
    assert host.failed


def test_radio_reset_requires_new_nonce_even_with_old_healthy_source():
    host, radio, clock = setup()
    arm(host, radio, clock)
    old = host.session
    clock.advance(.101)
    radio.incoming.append(b"AY1 00000000 1 8 0 M S\n")
    host.step(sample(clock))
    assert host.session != old and len(radio.commands()) == 1
    assert b"AB1 " in radio.writes[-1][1]


def test_explicit_selection_clear_ends_authority_until_reselected_and_rearmed():
    host, radio, clock = setup()
    arm(host, radio, clock)
    old = host.session
    clock.advance(.101)
    radio.report()
    host.step(sample(clock, valid=False, key=None))
    assert host.session != old and host.selection_key is None
    assert len(radio.commands()) == 1


def test_unsent_request_is_not_reported_as_radio_acceptance():
    host, radio, clock = setup()
    arm(host, radio, clock)
    assert host.reason == "correction requested: waiting for radio acceptance"
    clock.advance(.101)
    radio.report(state="A")
    host.step(sample(clock))
    assert host.reason == "radio reports assistance active"


def test_status_generation_wrap_accepts_zero_and_rejects_old_generation():
    host, radio, clock = setup()
    host.status = replace(host.status, generation=2**31 - 1, ticket=80)
    radio.gen = 0
    radio.report(ticket=1, state="M")
    host.step(sample(clock))
    assert host.status.generation == 0 and host.status.ticket == 1
    radio.gen = 2**31 - 1
    radio.report(ticket=90, state="A")
    host.step(sample(clock))
    assert host.status.generation == 0 and host.status.state == "M"


def test_wrapped_ticket_rejects_delayed_pre_wrap_status():
    host, radio, clock = setup()
    host.status = replace(host.status, ticket=2**31 - 1)
    radio.report(ticket=0)
    host.step(sample(clock))
    radio.report(ticket=2**31 - 2)
    host.step(sample(clock))
    assert host.status.ticket == 0


def test_radio_selection_transaction_binds_new_target_without_second_sc_cycle():
    host, radio, clock = setup()
    requested = []
    host.request_selection = requested.append
    session = host.session
    arm(host, radio, clock)
    token = f"{session}:{radio.gen}"
    assert requested == [token]
    assert host.selection_pending and radio.commands()[-1][1][-2:] == ["0", "0"]

    # The old request finishing after SC has no selection transaction marker.
    tick(host, radio, clock, advance=.101)
    assert radio.commands()[-1][1][-2:] == ["0", "0"]
    chosen = ("run", "camera", 19, 2)
    clock.advance(.101)
    radio.report(state="T", cause="T")
    result = stream.SelectionResult(token, clock(), chosen, True)
    fresh = replace(sample(clock, key=chosen), selection_result=result)
    host.step(fresh)
    assert host.session == session and host.selection_key == chosen
    assert not host.selection_pending and not host.selection_failed
    assert radio.commands()[-1][1][-2:] == ["1", "64"]
    assert len(radio.begins()) == 1 and requested == [token]

    # A later browser target change still requires a fresh manual cycle.
    clock.advance(.101)
    radio.report(state="A")
    host.step(sample(clock, key=("run", "camera", 20, 3)))
    assert host.session != session


def test_old_radio_selection_result_cannot_bind_after_a_second_sc_cycle():
    host, radio, clock = setup()
    requested = []
    host.request_selection = requested.append
    arm(host, radio, clock)
    old_token = requested[-1]
    old_result = stream.SelectionResult(old_token, clock(), ("old-selection",), True)
    clock.advance(.1)
    radio.gen += 1
    radio.report(state="M")
    host.step(sample(clock))
    clock.advance(.1)
    arm(host, radio, clock)
    assert requested[-1] != old_token
    clock.advance(.101)
    radio.report(state="T", cause="T")
    host.step(replace(sample(clock, key=("old-selection",)), selection_result=old_result))
    assert host.selection_pending and radio.commands()[-1][1][-2:] == ["0", "0"]


def test_failed_selection_stays_manual_until_new_operator_request():
    host, radio, clock = setup()
    requested = []
    host.request_selection = requested.append
    arm(host, radio, clock)
    failed = stream.SelectionResult(requested[-1], clock(), None, False)
    for _ in range(3):
        clock.advance(.101)
        radio.report(state="T", cause="T")
        host.step(replace(sample(clock), selection_result=failed))
        assert radio.commands()[-1][1][-2:] == ["0", "0"]
    assert host.selection_failed and not host.selection_pending
    assert "SC middle" in host.reason
    assert len(requested) == 1


def test_failed_selection_is_consumed_during_source_error_and_requires_new_cycle():
    host, radio, clock = setup()
    requested = []
    host.request_selection = requested.append
    arm(host, radio, clock)
    clock.advance(.101)
    radio.report(state="T", cause="T")
    failed = stream.SelectionResult(requested[-1], clock(), None, False)
    host.step(stream.SourceSample(None, clock(), True, failed))
    assert host.selection_failed and not host.selection_pending
    assert radio.commands()[-1][1][-2:] == ["0", "0"]

    # Subsequent valid GETs cannot turn that failed selection into an enable.
    tick(host, radio, clock, advance=.101)
    assert host.selection_failed and radio.commands()[-1][1][-2:] == ["0", "0"]
    assert "SC middle" in host.reason

    clock.advance(.101)
    radio.gen += 1
    radio.report(state="M")
    host.step(sample(clock))
    clock.advance(.101)
    arm(host, radio, clock)
    assert requested[-1] != failed.request_token
    clock.advance(.101)
    radio.report(state="T", cause="T")
    fresh = sample(clock)
    result = stream.SelectionResult(requested[-1], clock(), fresh.demand.selection_key, True)
    host.step(replace(fresh, selection_result=result))
    assert not host.selection_pending and not host.selection_failed
    assert radio.commands()[-1][1][-2:] == ["1", "64"]


def test_failed_selection_during_error_preserves_source_outage_session_rotation():
    host, radio, clock = setup()
    requested = []
    host.request_selection = requested.append
    arm(host, radio, clock)
    session = host.session
    failed = stream.SelectionResult(requested[-1], clock(), None, False)
    for _ in range(13):
        clock.advance(.101)
        state = "T" if host.session == session else "M"
        radio.report(state=state, cause=state)
        host.step(stream.SourceSample(None, clock(), True, failed))
    assert host.session != session and host.source_fault
    assert len(radio.begins()) == 2
    assert all(fields[-2:] == ["0", "0"] for _, fields in radio.commands())


@pytest.mark.parametrize("missing_demand", [False, True])
def test_successful_selection_waits_for_healthy_demand(missing_demand):
    host, radio, clock = setup()
    requested = []
    host.request_selection = requested.append
    arm(host, radio, clock)
    chosen = ("new-selection",)
    clock.advance(.101)
    radio.report(state="T", cause="T")
    result = stream.SelectionResult(requested[-1], clock(), chosen, True)
    current = sample(clock, key=chosen)
    broken = stream.SourceSample(None if missing_demand else current.demand,
                                 clock(), not missing_demand, result)
    host.step(broken)
    assert host.selection_pending and host.selection_key != chosen
    assert radio.commands()[-1][1][-2:] == ["0", "0"]
    clock.advance(.101)
    radio.report(state="T", cause="T")
    host.step(replace(sample(clock, key=chosen), selection_result=result))
    assert not host.selection_pending and host.selection_key == chosen
    assert radio.commands()[-1][1][-2:] == ["1", "64"]


def test_old_failed_selection_cannot_cancel_new_request_during_source_error():
    host, radio, clock = setup()
    requested = []
    host.request_selection = requested.append
    arm(host, radio, clock)
    old = stream.SelectionResult(requested[-1], clock(), None, False)
    clock.advance(.101)
    radio.gen += 1
    radio.report(state="M")
    host.step(sample(clock))
    clock.advance(.101)
    arm(host, radio, clock)
    clock.advance(.101)
    radio.report(state="T", cause="T")
    host.step(stream.SourceSample(None, clock(), True, old))
    assert host.selection_pending and not host.selection_failed
    assert radio.commands()[-1][1][-2:] == ["0", "0"]


@pytest.mark.parametrize("completed_offset", [-.01, .5, float("nan")])
def test_failed_selection_timestamps_are_checked_even_during_source_error(completed_offset):
    host, radio, clock = setup()
    requested = []
    host.request_selection = requested.append
    arm(host, radio, clock)
    failed = stream.SelectionResult(requested[-1], clock() + completed_offset, None, False)
    clock.advance(.101)
    radio.report(state="T", cause="T")
    with pytest.raises(stream.ProbeError, match="selection transaction"):
        host.step(stream.SourceSample(None, clock(), True, failed))
    assert host.failed


def test_lease_or_target_pause_does_not_create_an_operator_selection_event():
    host, radio, clock = setup()
    arm(host, radio, clock)
    requested = []
    host.request_selection = requested.append
    for state, cause in (("A", "A"), ("T", "E"), ("A", "A"), ("T", "T")):
        clock.advance(.101)
        radio.report(state=state, cause=cause)
        host.step(sample(clock))
    assert requested == []
    metrics = host.snapshot()
    assert metrics["radio_a_to_t_total"] == 2
    assert metrics["radio_a_to_t_causes"] == {"E": 1, "T": 1}
    assert metrics["radio_cause"] == "T"
    metrics["radio_a_to_t_causes"].clear()
    assert host.a_to_t_causes == {"E": 1, "T": 1}  # detached UI snapshot.


def test_old_radio_protocol_never_gets_a_host_write():
    clock = Clock()
    radio = Radio(clock)
    radio.incoming = deque([b"ARGOS_YAW_STREAM_V1\nAY1 abc012ef 1 1 0 M\n"])
    host = stream.YawStream(radio, clock=clock)
    host.step(sample(clock))
    clock.advance(5)
    with pytest.raises(stream.ProbeError, match="V2"):
        host.step(sample(clock))
    assert radio.writes == []


@pytest.mark.parametrize("diagnostic", ["model ARGOS_VIS", "internal_rf 5:0:8",
                                       "external_rf 5", "crash_flip 0", "selector 512",
                                       "stick 1100", "logical_switch nil",
                                       "api_exception getLogicalSwitchValue",
                                       "serial_api nil:function", "clock bad"])
def test_blocked_radio_diagnostic_is_reported_before_greeting_without_host_writes(diagnostic):
    clock = Clock()
    radio = Radio(clock)
    radio.incoming = deque([f"ARGOS_YAW_BLOCKED {diagnostic}\n".encode()])
    host = stream.YawStream(radio, clock=clock)
    with pytest.raises(stream.ProbeError, match=f"ArgFly blocked: {diagnostic.split()[0]}"):
        host.step(sample(clock))
    assert host.failed and not host.greeted
    assert host.snapshot()["reason"].startswith("ArgFly blocked:")
    assert radio.writes == []


def test_malformed_blocked_diagnostic_never_authorizes_or_writes():
    clock = Clock()
    radio = Radio(clock)
    radio.incoming = deque([b"ARGOS_YAW_BLOCKED unknown unsafe\n"])
    host = stream.YawStream(radio, clock=clock)
    with pytest.raises(stream.ProbeError, match="invalid.*diagnostic"):
        host.step(sample(clock))
    assert radio.writes == []


def test_worker_selection_is_async_and_result_survives_subsequent_reads():
    selected, release, second_read = threading.Event(), threading.Event(), threading.Event()
    key = ("new-selection",)

    class Source:
        reads = 0

        def read(self):
            self.reads += 1
            if self.reads >= 2:
                second_read.set()
            return YawDemand(20, True, key, time.monotonic() + .4, "tracking")

        def select_center(self, token):
            assert token == "abc012ef:4"
            selected.set()
            release.wait(1)
            return self.read()

        def close(self):
            pass

    worker = stream.SourceWorker(Source())
    assert worker.interval == .05
    worker.request_selection("abc012ef:4")
    worker.start()
    try:
        assert selected.wait(1)
        assert worker.snapshot().error  # The pending selection did not block snapshots.
        release.set()
        assert second_read.wait(1)
        # second_read is signalled within source.read, just before publication.
        deadline = time.monotonic() + 1
        while worker.metrics()["count"] < 2 and time.monotonic() < deadline:
            time.sleep(.001)
        result = worker.snapshot().selection_result
        assert result.request_token == "abc012ef:4" and result.success
        assert result.selection_key == key
        metrics = worker.metrics()
        assert metrics["count"] >= 2 and metrics["selections"] == 1
        assert metrics["wall_ms_total"] >= metrics["wall_ms_max"] >= 0
        assert metrics["cpu_ms_total"] >= metrics["cpu_ms_max"] >= 0
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize("selection", [False, True])
def test_worker_retains_bounded_error_diagnostic_after_recovery(selection):
    class Source:
        failed = False

        def fail_once(self):
            if not self.failed:
                self.failed = True
                raise ValueError("expired recovery\r\n" + "x" * 400)

        def read(self):
            if not selection:
                self.fail_once()
            return YawDemand(20, True, ("person",), time.monotonic() + .4, "tracking")

        def select_center(self, token):
            self.fail_once()

        def close(self):
            pass

    worker = stream.SourceWorker(Source(), interval=.001)
    assert worker.metrics()["last_error"] is None
    if selection:
        worker.request_selection("abc012ef:4")
    worker.start()
    try:
        deadline = time.monotonic() + 1
        while worker.metrics()["count"] < 2 and time.monotonic() < deadline:
            time.sleep(.001)
        metrics = worker.metrics()
        assert metrics["count"] >= 2 and metrics["errors"] == 1
        assert not worker.snapshot().error
        assert metrics["last_error"].startswith("ValueError: expired recovery  ")
        assert len(metrics["last_error"]) == 240
        assert "\n" not in metrics["last_error"] and "\r" not in metrics["last_error"]
        if selection:
            result = worker.snapshot().selection_result
            assert result.request_token == "abc012ef:4" and not result.success
    finally:
        worker.close()


def test_owned_shutdown_stops_reconnects_and_publishes_final_metrics(monkeypatch):
    clock = Clock()
    stop = threading.Event()

    class Worker:
        def __init__(self, source, **kwargs):
            pass

        def start(self):
            pass

        def snapshot(self):
            return sample(clock)

        def metrics(self):
            return {"count": 7, "wall_ms_total": 12., "cpu_ms_total": 2.}

        def close(self):
            pass

    class Port(Radio):
        def reset_input_buffer(self):
            pass

        def close(self):
            self.closed = True

    radio, statuses, opened = Port(clock), [], []

    def opener(device):
        opened.append(device)
        return radio

    def sleep(delta):
        clock.advance(delta)
        if clock() >= 100.2:
            stop.set()

    monkeypatch.setattr(stream, "SourceWorker", Worker)
    stream.run_stream("test-pocket", object(), opener=opener, clock=clock,
                      sleep=sleep, stop_event=stop, on_status=statuses.append,
                      report=lambda _: None)
    assert opened == ["test-pocket"] and radio.closed
    assert statuses[-1]["stopped"] and not statuses[-1]["connected"]
    assert statuses[-1]["source_poll"]["count"] == 7
