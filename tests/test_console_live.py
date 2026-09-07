"""Live inspection preserves payloads and remains bounded during silence/floods."""
import json
from types import MappingProxyType

import pytest

from argos.backends.mavlink import Received
from argos.console.live import LiveMessages


def event(at, *, system=1, component=1, kind=30, fields=None, frame=b"\xfd\x00"):
    return Received(at, system, component, 4, kind, "ATTITUDE" if kind == 30 else "HEARTBEAT",
                    fields or {"roll": .5}, frame)


def test_rates_expire_during_silence_without_another_message():
    live = LiveMessages()
    for index in range(30):
        live.append(event(index / 10))
    current, = live.snapshot(2.9)["types"]
    assert current["count"] == 30 and current["hz"] == 10.
    older, = live.snapshot(4.5)["types"]
    assert 4.3 < older["hz"] < 5.1
    absent, = live.snapshot(6.)["types"]
    assert absent["hz"] == 0. and absent["count"] == 30
    assert absent["last_received_at"] == 2.9 and absent["rx_age_s"] == 3.1
    assert absent["fields"] == {"roll": .5}


def test_source_and_type_are_independent_and_stably_sorted():
    live = LiveMessages()
    live.append(event(0., system=2))
    live.append(event(.1, component=42))
    live.append(event(.2))
    live.append(event(.3, kind=0))
    live.append(event(.4, fields={"roll": 2.}))
    types = live.snapshot(.4)["types"]
    assert [(item["system"], item["component"], item["message_id"]) for item in types] == [
        (1, 1, 0), (1, 1, 30), (1, 42, 30), (2, 1, 30)]
    assert [item["count"] for item in types] == [1, 2, 1, 1]
    assert types[1]["fields"] == {"roll": 2.}


def test_flood_retention_is_bounded_independently_of_frequency():
    live = LiveMessages(max_types=2)
    for index in range(30_000):
        live.append(event(index / 10000))
    assert len(live._types[(1, 1, 30)].buckets) <= 31
    assert sum(count for _, count in live._types[(1, 1, 30)].buckets) == 30_000
    live.append(event(3., kind=0))
    live.append(event(3.1))  # refresh the first identity; the other is evicted
    live.append(event(3.2, component=42))
    snap = live.snapshot(3.2)
    assert snap["evicted_types"] == 1 and snap["max_types"] == 2
    assert [(item["component"], item["message_id"]) for item in snap["types"]] == [(1, 30), (42, 30)]
    assert snap["types"][0]["count"] == 30_001
    live.append(event(3.3, kind=0))
    assert live.snapshot(3.3)["types"][0]["count"] == 1
    assert live.evicted_types == 2


def test_decoded_fields_remain_exact_and_json_safe_even_when_not_telemetry():
    live = LiveMessages()
    fields = MappingProxyType({"large": 2**64 - 1, "negative": -(2**63), "safe": 2**53 - 1,
                               "zero": 0., "bad": float("nan"),
                               "array": (float("inf"), -float("inf"), b"\x00\xff"),
                               "nested": MappingProxyType({"text": "<script>alert(1)</script>"})})
    live.append(event(0., fields=fields, frame=b"\xfe\x00\xff"))
    snap = live.snapshot(0.)
    encoded = json.dumps(snap, allow_nan=False)
    assert "18446744073709551615" in encoded
    item, = snap["types"]
    assert item["fields"]["large"] == "18446744073709551615"
    assert item["fields"]["negative"] == "-9223372036854775808"
    assert item["fields"]["safe"] == 2**53 - 1
    assert item["fields"]["bad"] == "NaN"
    assert item["fields"]["array"] == ["Infinity", "-Infinity", [0, 255]]
    assert (item["frame_hex"], item["frame_bytes"], item["wire_version"]) == ("fe00ff", 3, 1)
    item["fields"]["nested"]["text"] = "changed"
    assert live.snapshot(0.)["types"][0]["fields"]["nested"]["text"] == "<script>alert(1)</script>"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "1"])
def test_invalid_times_do_not_mutate_cache(bad):
    live = LiveMessages()
    live.append(event(1.))
    with pytest.raises(ValueError):
        live.append(event(bad))
    with pytest.raises(ValueError):
        live.snapshot(bad)
    assert live.snapshot(1.)["types"][0]["count"] == 1


def test_time_cannot_regress_across_reads_or_writes():
    live = LiveMessages()
    live.snapshot(2.)
    with pytest.raises(ValueError):
        live.append(event(1.))
    with pytest.raises(ValueError):
        live.snapshot(1.)
    assert live.snapshot(2.)["types"] == []


@pytest.mark.parametrize("kwargs", [{"window": 0}, {"window": -1}, {"max_types": 0},
                                    {"max_types": True}, {"max_types": 4097}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        LiveMessages(**kwargs)
