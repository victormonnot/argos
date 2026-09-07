"""Passive telemetry: source isolation and honest reception age across clocks.

Most checks exercise the dependency-free cache directly. The integration test
also passes actual MAVLink frames through the existing decoder, without I/O.
"""
from collections import deque
from dataclasses import FrozenInstanceError
import math

import pytest

from argos.backends.mavlink.link import Received
from argos.backends.mavlink.telemetry import (
    BootProgress,
    ReceptionState,
    TelemetryCache,
    TelemetryLimits,
    UpdateStatus,
)


def limits(**changes):
    values = dict(heartbeat=2., attitude=.5, local_position_ned=1.)
    values.update(changes)
    return TelemetryLimits(**values)


def cache(**changes):
    values = dict(system=1, component=1, limits=limits())
    values.update(changes)
    return TelemetryCache(**values)


def event(kind="HEARTBEAT", *, received_at=0., boot=1000, **changes):
    messages = {
        "HEARTBEAT": (0, dict(type=2, autopilot=3, base_mode=128,
                              custom_mode=0, system_status=4, mavlink_version=3)),
        "ATTITUDE": (30, dict(time_boot_ms=boot, roll=.1, pitch=-.2, yaw=.3,
                              rollspeed=.4, pitchspeed=-.5, yawspeed=.6)),
        "LOCAL_POSITION_NED": (32, dict(time_boot_ms=boot, x=10., y=20., z=-3.,
                                        vx=1., vy=2., vz=-.5)),
        "SYS_STATUS": (1, {}),
    }
    message_id, fields = messages[kind]
    fields["mavpackettype"] = kind
    values = dict(received_at=received_at, system=1, component=1, sequence=0,
                  message_id=message_id, type_name=kind, fields=fields,
                  frame=b"synthetic already-decoded frame")
    values.update(changes)
    return Received(**values)


def counts(snapshot):
    return (snapshot.accepted, snapshot.ignored_source,
            snapshot.ignored_type, snapshot.rejected)


def test_initial_snapshot_has_no_invented_vehicle_data():
    snapshot = cache(started_at=5.).snapshot(5.)
    assert (snapshot.at, snapshot.system, snapshot.component) == (5., 1, 1)
    assert counts(snapshot) == (0, 0, 0, 0)
    assert snapshot.last_rejection == ""
    for view in (snapshot.heartbeat, snapshot.attitude, snapshot.local_position_ned):
        assert view.message is None and view.rx_age is None
        assert view.state is ReceptionState.ABSENT
        assert view.boot_progress is None


@pytest.mark.parametrize("name", ["heartbeat", "attitude", "local_position_ned"])
@pytest.mark.parametrize("bad", [0., -1., math.nan, math.inf, True, None, "1"])
def test_each_reception_limit_must_be_an_explicit_positive_finite_duration(name, bad):
    with pytest.raises((TypeError, ValueError)):
        limits(**{name: bad})


def test_reception_limits_have_no_silent_defaults():
    with pytest.raises(TypeError):
        TelemetryLimits()


@pytest.mark.parametrize("field", ["system", "component"])
@pytest.mark.parametrize("bad", [0, 256, -1, True, 1., None])
def test_cache_requires_an_exact_valid_source(field, bad):
    with pytest.raises((TypeError, ValueError)):
        cache(**{field: bad})


def test_messages_have_independent_reception_dates_and_expiry_limits():
    telemetry = cache()
    for name in ("HEARTBEAT", "ATTITUDE", "LOCAL_POSITION_NED"):
        assert telemetry.update(event(name, received_at=1.), 1.).accepted
    first = telemetry.snapshot(1.5)
    assert first.heartbeat.state is ReceptionState.RECENT
    assert first.attitude.state is ReceptionState.RECENT
    assert first.local_position_ned.state is ReceptionState.RECENT
    just_after = telemetry.snapshot(math.nextafter(1.5, math.inf))
    assert just_after.attitude.state is ReceptionState.STALE
    assert just_after.local_position_ned.state is ReceptionState.RECENT
    assert telemetry.update(event(received_at=2.), 2.).accepted
    later = telemetry.snapshot(2.1)
    assert later.heartbeat.rx_age == pytest.approx(.1)
    assert later.heartbeat.state is ReceptionState.RECENT
    assert later.attitude.rx_age == pytest.approx(1.1)
    assert later.attitude.state is ReceptionState.STALE
    assert later.local_position_ned.state is ReceptionState.STALE
    assert later.local_position_ned.message.received_at == 1.


@pytest.mark.parametrize("kind,view_name,duration", [
    ("HEARTBEAT", "heartbeat", 2.),
    ("ATTITUDE", "attitude", .5),
    ("LOCAL_POSITION_NED", "local_position_ned", 1.),
])
def test_each_limit_includes_the_boundary_and_expires_immediately_after(kind, view_name, duration):
    telemetry = cache()
    telemetry.update(event(kind), 0.)
    assert getattr(telemetry.snapshot(duration), view_name).state is ReceptionState.RECENT
    later = getattr(telemetry.snapshot(math.nextafter(duration, math.inf)), view_name)
    assert later.state is ReceptionState.STALE
    assert later.message is not None  # historical data remain inspectable


def test_delayed_ingestion_keeps_reception_age_and_does_not_rebase_boot_time():
    telemetry = cache(started_at=20.)
    value = event("ATTITUDE", received_at=21., boot=4_000_000_000)
    assert telemetry.update(value, 25.).accepted
    view = telemetry.snapshot(25.).attitude
    assert view.rx_age == 4. and view.state is ReceptionState.STALE
    assert view.message.received_at == 21.
    assert view.message.fields["time_boot_ms"] == 4_000_000_000
    assert view.boot_progress is BootProgress.FIRST


def test_ned_coordinates_are_preserved_without_claiming_a_takeoff_origin():
    telemetry = cache()
    telemetry.update(event("LOCAL_POSITION_NED"), 0.)
    fields = telemetry.snapshot(0.).local_position_ned.message.fields
    assert tuple(fields[name] for name in ("x", "y", "z")) == (10., 20., -3.)
    assert tuple(fields[name] for name in ("vx", "vy", "vz")) == (1., 2., -.5)


def test_boot_progress_is_per_message_and_does_not_claim_to_identify_a_reboot():
    telemetry = cache()
    telemetry.update(event("ATTITUDE", boot=1000), 0.)
    telemetry.update(event("LOCAL_POSITION_NED", boot=10), 0.)
    first = telemetry.snapshot(0.)
    assert first.attitude.boot_progress is BootProgress.FIRST
    assert first.local_position_ned.boot_progress is BootProgress.FIRST
    steps = [(2000, BootProgress.ADVANCED), (2000, BootProgress.REPEATED),
             (1, BootProgress.DECREASED), (2, BootProgress.ADVANCED)]
    for index, (boot, progress) in enumerate(steps, 1):
        result = telemetry.update(event("ATTITUDE", received_at=index, boot=boot), index)
        assert result.accepted
        snapshot = telemetry.snapshot(index)
        assert snapshot.attitude.boot_progress is progress
        assert snapshot.attitude.message.fields["time_boot_ms"] == boot
        assert snapshot.attitude.rx_age == 0.
        assert snapshot.local_position_ned.boot_progress is BootProgress.FIRST
        assert snapshot.local_position_ned.message.fields["time_boot_ms"] == 10


def test_boot_wrap_is_reported_as_decreased_and_zero_is_a_valid_boot_stamp():
    telemetry = cache()
    telemetry.update(event("LOCAL_POSITION_NED", boot=2**32 - 1), 0.)
    assert telemetry.update(event("LOCAL_POSITION_NED", boot=0, received_at=1.), 1.).accepted
    snapshot = telemetry.snapshot(1.)
    assert snapshot.local_position_ned.boot_progress is BootProgress.DECREASED
    assert snapshot.local_position_ned.message.fields["time_boot_ms"] == 0


def test_heartbeat_exposes_reported_flags_without_boot_progress():
    telemetry = cache()
    telemetry.update(event(), 0.)
    view = telemetry.snapshot(0.).heartbeat
    assert view.message.fields["base_mode"] == 128
    assert view.message.fields["autopilot"] == 3
    assert view.boot_progress is None
    assert telemetry.snapshot(3.).heartbeat.state is ReceptionState.STALE


@pytest.mark.parametrize("change", [{"system": 2}, {"component": 2}])
def test_foreign_sources_neither_replace_samples_nor_advance_admission_clock(change):
    telemetry = cache()
    telemetry.update(event(), 0.)
    result = telemetry.update(event(received_at=100., **change), 100.)
    assert result.status is UpdateStatus.IGNORED_SOURCE and not result.accepted
    assert telemetry.update(event(received_at=1.), 1.).accepted
    snapshot = telemetry.snapshot(1.)
    assert counts(snapshot) == (2, 1, 0, 0)
    assert snapshot.heartbeat.message.system == snapshot.heartbeat.message.component == 1
    assert snapshot.heartbeat.message.received_at == 1.


def test_unrelated_message_type_does_not_advance_clock_or_invent_a_sample():
    telemetry = cache()
    result = telemetry.update(event("SYS_STATUS", received_at=100.), 100.)
    assert result.status is UpdateStatus.IGNORED_TYPE and not result.accepted
    assert telemetry.update(event(received_at=1.), 1.).accepted
    snapshot = telemetry.snapshot(1.)
    assert counts(snapshot) == (1, 0, 1, 0)
    assert snapshot.attitude.state is ReceptionState.ABSENT
    assert snapshot.local_position_ned.state is ReceptionState.ABSENT


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True, None, "1", 10**1000])
def test_invalid_call_time_raises_before_any_state_or_counter_change(bad):
    telemetry = cache()
    telemetry.update(event(), 0.)
    before = telemetry.snapshot(0.)
    with pytest.raises(ValueError):
        telemetry.update(event(received_at=1.), bad)
    with pytest.raises(ValueError):
        telemetry.snapshot(bad)
    after = telemetry.snapshot(0.)
    assert counts(after) == counts(before)
    assert after.last_rejection == before.last_rejection
    assert after.heartbeat.message == before.heartbeat.message
    assert telemetry.update(event(received_at=1.), 1.).accepted


def test_snapshot_advances_admission_clock_and_equal_dates_are_accepted():
    telemetry = cache()
    telemetry.snapshot(2.)
    with pytest.raises(ValueError):
        telemetry.update(event(received_at=1.), 1.)
    with pytest.raises(ValueError):
        telemetry.snapshot(1.)
    assert telemetry.update(event(received_at=1.), 2.).accepted
    assert telemetry.update(event("ATTITUDE", received_at=2.), 2.).accepted
    assert counts(telemetry.snapshot(2.)) == (2, 0, 0, 0)


@pytest.mark.parametrize("rx_at", [-1., 101., math.nan, math.inf, True, None])
def test_invalid_event_date_is_rejected_without_advancing_admission_clock(rx_at):
    telemetry = cache()
    telemetry.update(event("ATTITUDE"), 0.)
    result = telemetry.update(event("ATTITUDE", received_at=rx_at), 100.)
    assert result.status is UpdateStatus.REJECTED and not result.accepted
    assert result.detail
    snapshot = telemetry.snapshot(1.)
    assert snapshot.attitude.message.received_at == 0.
    assert snapshot.attitude.rx_age == 1.
    assert snapshot.attitude.state is ReceptionState.STALE
    assert counts(snapshot) == (1, 0, 0, 1)
    assert snapshot.last_rejection


def test_received_date_cannot_precede_a_nonzero_run_origin():
    telemetry = cache(started_at=50.)
    result = telemetry.update(event(received_at=49.), 50.)
    assert result.status is UpdateStatus.REJECTED
    assert telemetry.snapshot(50.).heartbeat.state is ReceptionState.ABSENT
    assert telemetry.update(event(received_at=50.), 50.).accepted


def test_older_arrival_of_same_type_cannot_refresh_or_replace_latest_sample():
    telemetry = cache()
    telemetry.update(event("ATTITUDE", received_at=2., boot=1000), 2.)
    older = event("ATTITUDE", received_at=1., boot=5000)
    assert telemetry.update(older, 100.).status is UpdateStatus.REJECTED
    # The rejected future-dated call must not prevent the next legitimate call.
    current = telemetry.snapshot(3.)
    assert current.attitude.message.received_at == 2.
    assert current.attitude.message.fields["time_boot_ms"] == 1000
    assert current.attitude.rx_age == 1.
    assert current.attitude.boot_progress is BootProgress.FIRST
    # A delayed *different* message type is independent of the attitude arrival.
    assert telemetry.update(event("LOCAL_POSITION_NED", received_at=1.), 3.).accepted
    assert telemetry.snapshot(3.).local_position_ned.rx_age == 2.


@pytest.mark.parametrize("kind,field,bad", [
    ("HEARTBEAT", "base_mode", True),
    ("HEARTBEAT", "base_mode", 256),
    ("HEARTBEAT", "custom_mode", -1),
    ("HEARTBEAT", "custom_mode", 2**32),
    ("ATTITUDE", "yaw", math.nan),
    ("ATTITUDE", "rollspeed", math.inf),
    ("ATTITUDE", "pitch", True),
    ("ATTITUDE", "time_boot_ms", True),
    ("ATTITUDE", "time_boot_ms", -1),
    ("ATTITUDE", "time_boot_ms", 2**32),
    ("LOCAL_POSITION_NED", "x", math.nan),
    ("LOCAL_POSITION_NED", "vy", -math.inf),
    ("LOCAL_POSITION_NED", "z", "-3"),
])
def test_malformed_payload_preserves_last_valid_sample_and_age(kind, field, bad):
    telemetry = cache()
    original = event(kind)
    telemetry.update(original, 0.)
    malformed = event(kind, received_at=100.)
    malformed.fields[field] = bad
    result = telemetry.update(malformed, 100.)
    assert result.status is UpdateStatus.REJECTED and result.detail
    view = getattr(telemetry.snapshot(3.), kind.lower())
    assert view.message.fields[field] == original.fields[field]
    assert view.message.received_at == 0. and view.rx_age == 3.
    assert view.state is ReceptionState.STALE
    assert telemetry.update(event(kind, received_at=4.), 4.).accepted
    assert telemetry.snapshot(4.).rejected == 1


@pytest.mark.parametrize("kind,field", [
    ("HEARTBEAT", "base_mode"),
    ("ATTITUDE", "yaw"),
    ("LOCAL_POSITION_NED", "vz"),
])
def test_missing_required_payload_fields_do_not_create_partial_samples(kind, field):
    telemetry = cache()
    malformed = event(kind)
    del malformed.fields[field]
    result = telemetry.update(malformed, 0.)
    assert result.status is UpdateStatus.REJECTED and result.detail
    view = getattr(telemetry.snapshot(0.), kind.lower())
    assert view.state is ReceptionState.ABSENT and view.message is None


@pytest.mark.parametrize("change", [
    {"system": True}, {"system": -1}, {"system": 256},
    {"component": True}, {"component": None},
    {"sequence": True}, {"sequence": -1}, {"sequence": 256},
    {"message_id": True}, {"message_id": 30}, {"type_name": "ATTITUDE"},
])
def test_invalid_or_inconsistent_headers_are_rejected_before_sample_mutation(change):
    telemetry = cache()
    result = telemetry.update(event(**change), 100.)
    assert result.status is UpdateStatus.REJECTED and result.detail
    snapshot = telemetry.snapshot(0.)
    assert counts(snapshot) == (0, 0, 0, 1)
    assert snapshot.heartbeat.state is ReceptionState.ABSENT


def test_payload_message_name_cannot_disagree_with_its_header():
    telemetry = cache()
    malformed = event()
    malformed.fields["mavpackettype"] = "ATTITUDE"
    assert telemetry.update(malformed, 0.).status is UpdateStatus.REJECTED
    assert telemetry.snapshot(0.).heartbeat.state is ReceptionState.ABSENT


@pytest.mark.parametrize("bad", [None, {}, b"raw frame, not a decoded event"])
def test_undecoded_input_is_a_diagnostic_rejection_without_advancing_clock(bad):
    telemetry = cache()
    result = telemetry.update(bad, 100.)
    assert result.status is UpdateStatus.REJECTED and result.detail
    assert telemetry.snapshot(0.).heartbeat.state is ReceptionState.ABSENT
    assert telemetry.update(event(), 0.).accepted


def test_equal_reception_dates_keep_the_last_valid_arrival_and_boot_diagnostic():
    telemetry = cache()
    telemetry.update(event("ATTITUDE", boot=100), 0.)
    next_arrival = event("ATTITUDE", boot=50, sequence=1)
    next_arrival.fields["yaw"] = -.7
    assert telemetry.update(next_arrival, 0.).accepted
    snapshot = telemetry.snapshot(0.)
    assert snapshot.attitude.message.sequence == 1
    assert snapshot.attitude.message.fields["yaw"] == -.7
    assert snapshot.attitude.boot_progress is BootProgress.DECREASED
    assert snapshot.attitude.rx_age == 0.


def test_rejection_history_survives_later_valid_receptions():
    telemetry = cache()
    invalid = event("ATTITUDE")
    del invalid.fields["yaw"]
    result = telemetry.update(invalid, 100.)
    assert result.status is UpdateStatus.REJECTED
    assert telemetry.update(event("ATTITUDE"), 0.).accepted
    snapshot = telemetry.snapshot(0.)
    assert snapshot.last_rejection == result.detail
    assert counts(snapshot) == (1, 0, 0, 1)


def test_samples_and_old_snapshots_are_isolated_from_mutable_input():
    telemetry = cache()
    value = event(frame=b"original")
    assert telemetry.update(value, 0.).accepted
    first = telemetry.snapshot(0.)
    value.fields["base_mode"] = 0
    stored = first.heartbeat.message
    assert stored.fields["base_mode"] == 128 and stored.frame == b"original"
    assert isinstance(stored.frame, bytes)
    with pytest.raises(TypeError):
        stored.fields["base_mode"] = 0
    with pytest.raises((FrozenInstanceError, AttributeError)):
        first.at = 99.
    with pytest.raises((FrozenInstanceError, AttributeError)):
        first.heartbeat.rx_age = 99.
    with pytest.raises((FrozenInstanceError, AttributeError)):
        stored.received_at = 99.
    assert telemetry.update(event(received_at=1.), 1.).accepted
    second = telemetry.snapshot(1.)
    assert first.at == first.heartbeat.rx_age == 0.
    assert first.heartbeat.message.received_at == 0.
    assert first.accepted == 1 and second.accepted == 2
    assert second.heartbeat.message.received_at == 1.


def test_mutable_frame_is_rejected_at_the_decoded_event_boundary():
    telemetry = cache()
    assert telemetry.update(event(frame=bytearray(b"mutable")), 100.).status is UpdateStatus.REJECTED
    assert telemetry.snapshot(0.).heartbeat.state is ReceptionState.ABSENT
    assert telemetry.update(event(), 0.).accepted


class MemoryTransport:
    datagram = False

    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.writes = []
        self.closed = False

    def read(self):
        item = self.chunks.popleft() if self.chunks else b""
        if isinstance(item, OSError):
            raise item
        return item

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("v1", [False, True])
def test_real_decoding_keeps_full_link_statistics_and_never_transmits(v1):
    mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
    from argos.backends.mavlink import MavlinkLink, SequenceScope

    heartbeat = mav.MAVLink_heartbeat_message(2, 3, 128, 0, 4, 3)
    attitude = mav.MAVLink_attitude_message(456, .1, -.2, .3, .4, -.5, .6)
    position = mav.MAVLink_local_position_ned_message(123, 10., 20., -3., 1., 2., -.5)
    unrelated = mav.MAVLink_ping_message(100, 1, 0, 0)
    raw = []
    for seq, (message, system, component) in enumerate([
        (heartbeat, 1, 1), (attitude, 1, 1), (position, 1, 1),
        (heartbeat, 1, 42), (heartbeat, 2, 1), (unrelated, 1, 1),
    ]):
        encoder = mav.MAVLink(None, srcSystem=system, srcComponent=component)
        encoder.seq = seq
        raw.append(message.pack(encoder, force_mavlink1=v1))
    transport = MemoryTransport([b"".join(raw)])
    telemetry = cache()
    with MavlinkLink(transport, sequence_scope=SequenceScope.CHANNEL) as wire:
        received = wire.poll(1.)
        assert len(received) == 6
        statuses = [telemetry.update(item, 1.).status for item in received]
        assert statuses == [UpdateStatus.ACCEPTED] * 3 + [
            UpdateStatus.IGNORED_SOURCE, UpdateStatus.IGNORED_SOURCE,
            UpdateStatus.IGNORED_TYPE,
        ]
        snapshot = telemetry.snapshot(1.)
        assert counts(snapshot) == (3, 2, 1, 0)
        assert snapshot.attitude.message.fields["yaw"] == pytest.approx(.3)
        assert snapshot.local_position_ned.message.fields["z"] == -3.
        assert snapshot.attitude.message.fields["time_boot_ms"] == 456
        assert snapshot.local_position_ned.message.fields["time_boot_ms"] == 123
        report = wire.report(1.)
        assert report.traffic.rx == 6 and report.traffic.sequence.loss == 0.
        assert report.traffic.tx == 0 and report.traffic.tx_attempts == 0
        assert transport.writes == []
    assert transport.closed and transport.writes == []
    assert telemetry.snapshot(10.).heartbeat.state is ReceptionState.STALE


def test_corrupt_wire_payload_cannot_refresh_cached_telemetry():
    mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
    from argos.backends.mavlink import MavlinkLink, SequenceScope

    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    message = mav.MAVLink_attitude_message(100, 0., 0., .3, 0., 0., 0.)
    raw = message.pack(encoder)
    corrupt = bytearray(raw)
    corrupt[-1] ^= 0xff
    transport = MemoryTransport([raw])
    telemetry = cache()
    with MavlinkLink(transport, sequence_scope=SequenceScope.CHANNEL) as wire:
        for item in wire.poll(0.):
            assert telemetry.update(item, 0.).accepted
        transport.chunks.append(bytes(corrupt))
        assert wire.poll(10.) == ()
        snapshot = telemetry.snapshot(10.)
        assert snapshot.attitude.state is ReceptionState.STALE
        assert snapshot.attitude.rx_age == 10.
        assert snapshot.accepted == 1 and snapshot.rejected == 0
        assert wire.report(10.).bad_bytes > 0
        assert transport.writes == []


def test_valid_crc_with_nan_counts_as_link_traffic_but_cannot_refresh_telemetry():
    mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
    from argos.backends.mavlink import MavlinkLink, SequenceScope

    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    valid = mav.MAVLink_attitude_message(100, 0., 0., .3, 0., 0., 0.).pack(encoder)
    encoder.seq = 1
    malformed = mav.MAVLink_attitude_message(200, 0., 0., math.nan, 0., 0., 0.).pack(encoder)
    transport = MemoryTransport([valid])
    telemetry = cache()
    with MavlinkLink(transport, sequence_scope=SequenceScope.CHANNEL) as wire:
        incoming, = wire.poll(0.)
        assert telemetry.update(incoming, 0.).accepted
        transport.chunks.append(malformed)
        incoming, = wire.poll(1.)
        assert telemetry.update(incoming, 1.).status is UpdateStatus.REJECTED
        snapshot = telemetry.snapshot(1.)
        assert snapshot.attitude.message.fields["yaw"] == pytest.approx(.3)
        assert snapshot.attitude.message.fields["time_boot_ms"] == 100
        assert snapshot.attitude.state is ReceptionState.STALE
        assert snapshot.attitude.rx_age == 1.
        assert counts(snapshot) == (1, 0, 0, 1)
        report = wire.report(1.)
        assert report.traffic.rx == 2 and report.bad_bytes == 0
        assert report.traffic.sequence.loss == 0.
        assert transport.writes == []


def test_read_failure_preserves_valid_prefix_and_receptions_keep_aging_after_close():
    mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
    from argos.backends.mavlink import MavlinkLink, SequenceScope

    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    raw = mav.MAVLink_heartbeat_message(2, 3, 128, 0, 4, 3).pack(encoder)
    transport = MemoryTransport([raw, OSError("test disconnected stream")])
    telemetry = cache()
    with MavlinkLink(transport, sequence_scope=SequenceScope.CHANNEL) as wire:
        incoming, = wire.poll(1.)
        assert telemetry.update(incoming, 1.).accepted
        report = wire.report(1.)
        assert report.closed and report.read_errors == 1
        assert report.traffic.rx == 1 and report.traffic.tx == 0
        assert "disconnected" in report.last_error
        assert telemetry.snapshot(1.).heartbeat.state is ReceptionState.RECENT
        assert wire.poll(10.) == ()
        later = telemetry.snapshot(10.)
        assert later.heartbeat.message.received_at == 1.
        assert later.heartbeat.rx_age == 9.
        assert later.heartbeat.state is ReceptionState.STALE
        assert counts(later) == (1, 0, 0, 0)
        assert transport.closed and transport.writes == []
