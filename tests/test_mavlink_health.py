"""Battery sentinels, local admission clocks and explicitly scoped mode labels."""
from collections import deque
from dataclasses import FrozenInstanceError, replace
import json
import math
import subprocess
import sys

import pytest

from argos.backends.mavlink.health import HealthCache, interpret_mode
from argos.backends.mavlink.link import MavlinkLink, Received, SequenceScope
from argos.backends.mavlink.telemetry import ReceptionState, UpdateStatus


def event(*, at=1., **fields):
    payload = dict(voltage_battery=12400, current_battery=230, battery_remaining=75)
    payload.update(fields)
    return Received(at, 1, 1, 0, 1, "SYS_STATUS", payload, b"already decoded")


def cache(**options):
    return HealthCache(system=1, component=1, **options)


def heartbeat(**changes):
    fields = dict(type=2, autopilot=3, base_mode=1, custom_mode=0)
    fields.update(changes)
    return fields


def test_absence_limits_and_stale_values_preserve_the_original_receipt():
    health = cache(started_at=5., age_limit=2.)
    absent = health.snapshot(5.).battery
    assert absent.message is absent.rx_age is absent.voltage_v is None
    assert absent.current_a is absent.remaining_percent is None
    assert absent.state is ReceptionState.ABSENT
    assert health.update(event(at=6.), 7.).accepted
    boundary = health.snapshot(8.).battery
    assert boundary.rx_age == 2. and boundary.state is ReceptionState.RECENT
    stale = health.snapshot(math.nextafter(8., math.inf)).battery
    assert stale.state is ReceptionState.STALE
    assert stale.message.received_at == 6.
    assert (stale.voltage_v, stale.current_a, stale.remaining_percent) == (12.4, 2.3, 75)


@pytest.mark.parametrize("fields,expected", [
    ({"voltage_battery": 65535, "current_battery": -1, "battery_remaining": -1},
     (None, None, None)),
    ({"voltage_battery": 0, "current_battery": 0, "battery_remaining": 0}, (0., 0., 0)),
    ({"voltage_battery": 65535, "current_battery": 100, "battery_remaining": 100},
     (None, 1., 100)),
    ({"voltage_battery": 65534, "current_battery": -32768, "battery_remaining": -1},
     (65.534, -327.68, None)),
])
def test_each_sentinel_is_independent_from_measured_zero_and_signed_current(fields, expected):
    health = cache()
    assert health.update(event(**fields), 1.).accepted
    battery = health.snapshot(1.).battery
    assert (battery.voltage_v, battery.current_a, battery.remaining_percent) == expected
    assert battery.state is ReceptionState.RECENT


@pytest.mark.parametrize("field,bad", [
    ("voltage_battery", -1), ("voltage_battery", 65536),
    ("current_battery", -32769), ("current_battery", 32768),
    ("battery_remaining", -2), ("battery_remaining", 101),
    ("voltage_battery", True), ("current_battery", 1.5),
    ("battery_remaining", math.nan),
])
def test_invalid_payload_is_atomic_and_does_not_move_admission_clock(field, bad):
    health = cache()
    health.update(event(), 1.)
    assert health.update(event(at=100., **{field: bad}), 100.).status is UpdateStatus.REJECTED
    # A refusal made at a future call date must not poison the valid stream.
    assert health.update(event(at=2., battery_remaining=74), 2.).accepted
    snapshot = health.snapshot(2.)
    assert snapshot.battery.remaining_percent == 74
    assert snapshot.accepted == 2 and snapshot.rejected == 1
    assert field in snapshot.last_rejection


@pytest.mark.parametrize("changes", [
    {"received_at": 10.}, {"received_at": -1.}, {"received_at": math.nan},
    {"system": True}, {"component": 256}, {"sequence": -1},
    {"message_id": 0}, {"message_id": True}, {"type_name": "HEARTBEAT"},
    {"fields": None}, {"fields": {"voltage_battery": 1}}, {"frame": bytearray()},
])
def test_bad_event_metadata_or_missing_payload_preserves_last_sample(changes):
    health = cache()
    health.update(event(), 1.)
    original = health.snapshot(1.).battery
    assert health.update(replace(event(at=2.), **changes), 2.).status is UpdateStatus.REJECTED
    assert health.snapshot(1.).battery == original


def test_wrong_payload_name_and_non_event_are_rejected():
    health = cache()
    assert health.update(event(mavpackettype="ATTITUDE"), 1.).status is UpdateStatus.REJECTED
    assert health.update(None, 1.).status is UpdateStatus.REJECTED
    assert health.snapshot(0.).rejected == 2


def test_sources_and_other_types_do_not_advance_the_clock():
    health = cache()
    other = replace(event(at=100.), component=2)
    assert health.update(other, 100.).status is UpdateStatus.IGNORED_SOURCE
    other_type = replace(event(at=100.), message_id=0, type_name="HEARTBEAT", fields={})
    assert health.update(other_type, 100.).status is UpdateStatus.IGNORED_TYPE
    assert health.update(event(), 1.).accepted
    snap = health.snapshot(1.)
    assert snap.accepted == snap.ignored_source == snap.ignored_type == 1
    assert snap.rejected == 0


def test_out_of_order_receipts_are_refused_equal_receipts_are_valid():
    health = cache()
    health.update(event(at=2.), 3.)
    assert health.update(event(at=1.), 4.).status is UpdateStatus.REJECTED
    assert health.update(event(at=2., battery_remaining=74), 3.).accepted
    assert health.snapshot(3.).battery.remaining_percent == 74


def test_snapshots_and_payloads_are_detached_and_frozen():
    health = cache()
    packet = event()
    health.update(packet, 1.)
    snapshot = health.snapshot(1.)
    packet.fields["battery_remaining"] = 1
    assert snapshot.battery.remaining_percent == 75
    assert snapshot.battery.message.fields["battery_remaining"] == 75
    with pytest.raises(TypeError):
        snapshot.battery.message.fields["voltage_battery"] = 0
    with pytest.raises(FrozenInstanceError):
        snapshot.battery.current_a = 1.


@pytest.mark.parametrize("options", [
    {"age_limit": 0}, {"age_limit": math.nan}, {"age_limit": True},
    {"started_at": -1}, {"started_at": math.inf},
])
def test_bad_configuration_is_refused(options):
    with pytest.raises(ValueError):
        cache(**options)


def test_cache_requires_exact_source_and_valid_call_clock():
    for source in ({"system": 0, "component": 1}, {"system": 1, "component": True}):
        with pytest.raises(ValueError):
            HealthCache(**source)
    health = cache()
    health.update(event(), 1.)
    initial = health.snapshot(2.)
    for bad in (1., math.nan, math.inf, True):
        with pytest.raises(ValueError):
            health.update(event(at=2.), bad)
        with pytest.raises(ValueError):
            health.snapshot(bad)
    assert health.snapshot(2.) == initial


def test_real_encoded_sys_status_and_heartbeat_are_received_without_io_or_emission():
    mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    status = encoder.sys_status_encode(0, 0, 0, 0, 12000, -1, 50, 0, 0, 0, 0, 0, 0)
    beat = encoder.heartbeat_encode(mav.MAV_TYPE_QUADROTOR,
                                   mav.MAV_AUTOPILOT_ARDUPILOTMEGA, 1, 5, 4)

    class MemoryInput:
        datagram = True

        def __init__(self):
            self.frames = deque([status.pack(encoder) + beat.pack(encoder)])

        def read(self, max_bytes=65535):
            return self.frames.popleft() if self.frames else b""

        def write(self, data):
            raise AssertionError("passive telemetry must not transmit")

        def close(self):
            pass

    health = cache()
    with MavlinkLink(MemoryInput(), sequence_scope=SequenceScope.CHANNEL) as link:
        received = link.poll(1.)
        assert [item.type_name for item in received] == ["SYS_STATUS", "HEARTBEAT"]
        for packet in received:
            health.update(packet, 1.)
        battery = health.snapshot(1.).battery
        assert (battery.voltage_v, battery.current_a, battery.remaining_percent) == (12., None, 50)
        assert interpret_mode(received[1].fields)["label"] == "LOITER"
        assert link.report(1.).traffic.tx_attempts == 0


@pytest.mark.parametrize("vehicle", [2, 3, 4, 13, 14, 15, 29])
def test_explicit_rotorcraft_types_use_installed_copter_mode_names(vehicle):
    pytest.importorskip("pymavlink")
    result = interpret_mode(heartbeat(type=vehicle, custom_mode=2))
    assert result["label"] == "ALT_HOLD" and result["known"]
    assert result["mapping"] == "arducopter"
    assert result["vehicle_type"] == vehicle and result["custom_mode"] == 2
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("fields", [
    heartbeat(autopilot=12), heartbeat(type=1), heartbeat(type=10),
    heartbeat(type=0), heartbeat(type=26), heartbeat(base_mode=128),
])
def test_other_vehicle_firmware_or_disabled_custom_flag_never_uses_copter_mapping(fields):
    result = interpret_mode(fields)
    assert result["label"] == "Unknown (0)" and not result["known"]
    assert result["mapping"] is None and result["custom_mode"] == 0
    assert result["base_mode"] == fields["base_mode"]
    assert result["autopilot"] == fields["autopilot"]


def test_unknown_mode_keeps_exact_raw_id():
    pytest.importorskip("pymavlink")
    result = interpret_mode(heartbeat(custom_mode=2**32 - 1))
    assert result["label"] == "Unknown (4294967295)"
    assert result["custom_mode"] == 4294967295 and not result["known"]


@pytest.mark.parametrize("fields", [None, {}, heartbeat(custom_mode=True),
                                         heartbeat(type=256), heartbeat(base_mode=-1)])
def test_malformed_mode_fields_raise_instead_of_inventing_a_label(fields):
    with pytest.raises(ValueError):
        interpret_mode(fields)


def test_health_import_and_unsupported_mode_do_not_import_optional_codec():
    code = (
        "import sys; from argos.backends.mavlink.health import HealthCache, interpret_mode; "
        "HealthCache(system=1, component=1); "
        "interpret_mode(dict(type=1,autopilot=3,base_mode=1,custom_mode=0)); "
        "assert not any(n == 'pymavlink' or n.startswith('pymavlink.') for n in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
