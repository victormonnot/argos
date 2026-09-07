"""Passive status text assembly: exact wire bytes, missing data and source epochs."""
from dataclasses import replace
import json

import pytest

from argos.backends.mavlink import Received
from argos.console.status import StatusTexts, system_status_view

mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")


def event(at=0., text=b"Received", *, severity=6, status_id=0, chunk=0,
          sequence=10, system=1, component=1, version=2):
    if version == 1:
        from pymavlink.dialects.v10 import ardupilotmega as dialect
        message = dialect.MAVLink_statustext_message(severity, text)
    else:
        dialect = mav
        message = mav.MAVLink_statustext_message(severity, text, id=status_id, chunk_seq=chunk)
    encoder = dialect.MAVLink(None, srcSystem=system, srcComponent=component)
    encoder.seq = sequence
    frame = message.pack(encoder)
    decoded = mav.MAVLink(None).parse_char(frame)
    assert decoded.get_msgId() == 253
    return Received(at, system, component, sequence, 253, "STATUSTEXT", decoded.to_dict(), frame)


def history(**kwargs):
    return StatusTexts(system=1, component=1, connection_id="connection-a", **kwargs)


def entries(hist, now=0.):
    return hist.snapshot(now)["entries"]


def test_no_messages_is_unknown_history_not_a_health_declaration():
    hist = history()
    snap = hist.snapshot(100.)
    assert snap["entries"] == [] and snap["pending"] == 0
    assert "healthy" not in snap and "state" not in snap
    assert snap["system"] == 1 and snap["component"] == 1


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("text", [b"short", b"A" * 50, b"", b"prefix\0ignored"])
def test_standalone_messages_need_no_terminator_or_later_poll(version, text):
    hist = history()
    received = event(2., text, version=version)
    assert hist.append(received, now=2.)
    item, = entries(hist, 12.)
    assert item["text"] == text.split(b"\0")[0].decode()
    assert item["complete"] and item["reason"] is None and item["chunks"] == 1
    assert item["first_received_at"] == item["received_at"] == 2.
    assert item["rx_age_s"] == 10. and item["connection_id"] == "connection-a"
    assert hist.snapshot(12.)["pending"] == 0


def test_utf8_bytes_override_lossy_pymavlink_ascii_and_are_html_text_only():
    hist = history()
    message = event(text="Étalonnage <img src=x onerror=alert(1)>".encode())
    assert "É" not in message.fields["text"]  # pinned codec currently loses the accent
    hist.append(message, now=0.)
    item, = entries(hist)
    assert item["text"] == "Étalonnage <img src=x onerror=alert(1)>"
    assert item["utf8_valid"] is True
    # The backend supplies text, not an HTML fragment; frontend uses textContent.
    json.dumps(hist.snapshot(0.), allow_nan=False)


def test_utf8_character_split_between_chunks_is_decoded_after_assembly():
    hist = history()
    first = b"A" * 49 + b"\xc3"
    hist.append(event(0., first, status_id=41), now=0.)
    pending, = entries(hist)
    assert pending["reason"] == "assembling" and pending["utf8_valid"] is False
    hist.append(event(.1, b"\xa9 fin", status_id=41, chunk=1, sequence=11), now=.1)
    item, = entries(hist, .1)
    assert item["text"] == "A" * 49 + "é fin"
    assert item["utf8_valid"] and item["complete"] and item["chunks"] == 2
    assert item["first_received_at"] == 0. and item["received_at"] == .1


def test_exact_multiple_of_fifty_needs_final_empty_chunk_for_nonzero_id():
    hist = history()
    hist.append(event(text=b"A" * 50, status_id=9), now=0.)
    hist.append(event(.1, b"B" * 50, status_id=9, chunk=1, sequence=11), now=.1)
    assert not entries(hist, .1)[0]["complete"]
    hist.append(event(.2, b"", status_id=9, chunk=2, sequence=12), now=.2)
    item, = entries(hist, .2)
    assert item["complete"] and item["text"] == "A" * 50 + "B" * 50
    assert item["chunks"] == 3


def test_silence_expires_at_explicit_boundary_without_redating_partial_text():
    hist = history(chunk_timeout_s=2.)
    hist.append(event(3., b"A" * 50, status_id=9), now=3.)
    assert entries(hist, 4.999)[0]["reason"] == "assembling"
    expired, = entries(hist, 5.)
    assert not expired["complete"] and expired["reason"] == "timeout"
    assert expired["received_at"] == 3. and expired["rx_age_s"] == 2.
    hist.append(event(5.1, b"late end", status_id=9, chunk=1), now=5.1)
    items = entries(hist, 5.1)
    assert len(items) == 2
    assert items[0]["reason"] == "missing_chunk" and items[0]["text"] == "late end"
    assert items[1]["reason"] == "timeout"


def test_duplicate_pending_chunk_does_not_extend_deadline_or_text():
    hist = history()
    first = event(text=b"A" * 50, status_id=8)
    hist.append(first, now=0.)
    hist.append(replace(first, received_at=1.5), now=1.5)
    item, = entries(hist, 1.5)
    assert item["text"] == "A" * 50 and item["received_at"] == 0.
    assert hist.snapshot(1.5)["duplicates"] == 1
    assert entries(hist, 2.)[0]["reason"] == "timeout"


def test_repeated_standalone_reports_remain_distinct_historical_receipts():
    hist = history()
    message = event()
    hist.append(message, now=0.)
    hist.append(replace(message, received_at=1.), now=1.)
    assert len(entries(hist, 1.)) == 2  # no evidence that ID-zero reports are one event


def test_missing_middle_is_never_silently_joined_to_prefix():
    hist = history()
    hist.append(event(text=b"prefix" * 8 + b"!!", status_id=8), now=0.)
    hist.append(event(.1, b"suffix", status_id=8, chunk=2), now=.1)
    items = entries(hist, .1)
    assert [item["reason"] for item in items] == ["missing_chunk", "missing_chunk"]
    assert all(not item["complete"] for item in items)
    assert items[0]["text"] == "suffix" and "suffix" not in items[1]["text"]
    assert hist.snapshot(.1)["pending"] == 0


def test_out_of_order_start_does_not_absorb_an_earlier_orphan_fragment():
    hist = history()
    hist.append(event(text=b"end", status_id=8, chunk=1), now=0.)
    hist.append(event(.1, b"A" * 50, status_id=8, chunk=0), now=.1)
    items = entries(hist, .1)
    assert items[0]["reason"] == "assembling"
    assert items[1]["reason"] == "missing_chunk"


def test_reusing_id_with_new_start_closes_old_partial_before_new_text():
    hist = history()
    hist.append(event(text=b"A" * 50, status_id=8), now=0.)
    hist.append(event(.1, b"B" * 50, status_id=8, sequence=11), now=.1)
    hist.append(event(.2, b"end", status_id=8, chunk=1, sequence=12), now=.2)
    new, old = entries(hist, .2)
    assert old["reason"] == "restarted" and not old["complete"]
    assert new["complete"] and new["text"] == "B" * 50 + "end"


def test_same_prefix_with_new_transport_sequence_is_not_assumed_a_duplicate():
    hist = history()
    hist.append(event(text=b"A" * 50, status_id=8), now=0.)
    hist.append(event(.1, b"A" * 50, status_id=8, sequence=11), now=.1)
    new, old = entries(hist, .1)
    assert new["reason"] == "assembling" and old["reason"] == "restarted"


def test_conflicting_repeat_or_severity_closes_both_pieces_as_incomplete():
    for severity, repeated_chunk, reason in ((6, 1, "conflicting_chunk"), (2, 2, "severity_changed")):
        hist = history()
        hist.append(event(text=b"A" * 50, status_id=8), now=0.)
        hist.append(event(.1, b"B" * 50, status_id=8, chunk=1), now=.1)
        hist.append(event(.2, b"different", status_id=8, chunk=repeated_chunk, severity=severity), now=.2)
        items = entries(hist, .2)
        assert len(items) == 2 and all(item["reason"] == reason for item in items)
        assert all(not item["complete"] for item in items)


def test_other_origins_and_ids_cannot_complete_each_others_messages():
    hist = history()
    hist.append(event(text=b"A" * 50, status_id=8), now=0.)
    assert not hist.append(event(.1, b"wrong system", status_id=8, chunk=1, system=2), now=.1)
    assert not hist.append(event(.1, b"wrong component", status_id=8, chunk=1, component=2), now=.1)
    hist.append(event(.1, b"other id", status_id=9, chunk=1), now=.1)
    hist.append(event(.2, b"real end", status_id=8, chunk=1), now=.2)
    items = entries(hist, .2)
    assert items[0]["text"] == "A" * 50 + "real end" and items[0]["complete"]
    assert items[1]["reason"] == "missing_chunk" and items[1]["status_id"] == 9
    assert hist.snapshot(.2)["rejected"] == 0


def test_interleaved_ids_complete_independently_in_last_reception_order():
    hist = history()
    hist.append(event(0., b"A" * 50, status_id=8), now=0.)
    hist.append(event(.1, b"B" * 50, status_id=9), now=.1)
    hist.append(event(.2, b"end B", status_id=9, chunk=1), now=.2)
    hist.append(event(.3, b"end A", status_id=8, chunk=1), now=.3)
    a, b = entries(hist, .3)
    assert a["text"] == "A" * 50 + "end A" and a["complete"]
    assert b["text"] == "B" * 50 + "end B" and b["complete"]
    assert a["first_received_at"] == 0. and b["first_received_at"] == .1
    assert a["received_at"] == .3 and b["received_at"] == .2


def test_reconnect_preserves_history_but_never_joins_across_connection_epoch():
    hist = history()
    hist.append(event(text=b"old complete"), now=0.)
    hist.append(event(.1, b"A" * 50, status_id=8), now=.1)
    hist.reconnect("connection-b", 1.)
    assert entries(hist, 1.)[0]["reason"] == "reconnect"
    assert not hist.append(event(.2, b"late old bytes", status_id=8, chunk=1), now=1.)
    hist.append(event(1.1, b"new suffix", status_id=8, chunk=1), now=1.1)
    snap = hist.snapshot(1.1)
    assert snap["connection_id"] == "connection-b"
    assert [item["connection_id"] for item in snap["entries"]] == ["connection-b", "connection-a", "connection-a"]
    assert snap["entries"][0]["reason"] == "missing_chunk"
    assert snap["entries"][1]["received_at"] == .1
    assert snap["entries"][2]["complete"]


@pytest.mark.parametrize("connection_id", ["connection-a", "", "x" * 129, None])
def test_invalid_reconnect_does_not_close_pending_text_or_change_epoch(connection_id):
    hist = history()
    hist.append(event(text=b"A" * 50, status_id=8), now=0.)
    before = hist.snapshot(0.)
    with pytest.raises(ValueError):
        hist.reconnect(connection_id, 1.)
    assert hist.snapshot(0.) == before


def test_fragment_and_pending_caps_are_visible_and_history_remains_bounded():
    hist = history(max_entries=3, max_pending=2, max_chunks=2)
    for identifier in (1, 2, 3):
        hist.append(event(0., b"A" * 50, status_id=identifier), now=0.)
    snap = hist.snapshot(0.)
    assert snap["pending"] == 2 and snap["entries"][-1]["reason"] == "limit"
    hist.append(event(.1, b"B" * 50, status_id=3, chunk=1), now=.1)
    assert entries(hist, .1)[0]["reason"] == "limit"
    for index in range(1000):
        hist.append(event(.2, b"bounded"), now=.2)
    snap = hist.snapshot(.2)
    assert len(snap["entries"]) == 3 and snap["evicted_entries"] == 1000
    assert snap["pending"] == 0
    assert len(hist._pending) == 0 and len(hist._entries) == 3


def test_evicted_pending_message_cannot_complete_invisibly_or_reappear():
    hist = history(max_entries=1, max_pending=1)
    hist.append(event(text=b"A" * 50, status_id=8), now=0.)
    hist.append(event(.1, b"new standalone"), now=.1)
    hist.append(event(.2, b"old suffix", status_id=8, chunk=1), now=.2)
    item, = entries(hist, .2)
    assert item["reason"] == "missing_chunk" and item["text"] == "old suffix"
    assert hist.snapshot(.2)["evicted_entries"] == 2


@pytest.mark.parametrize("severity", [0, 1, 2, 3, 4, 5, 6, 7, 8, 255])
def test_known_and_unknown_severities_preserve_declared_numeric_value(severity):
    hist = history()
    hist.append(event(severity=severity), now=0.)
    item, = entries(hist)
    assert item["severity"] == severity and item["complete"]
    assert (item["severity_name"] is not None) == (severity < 8)
    if severity >= 8:
        assert str(severity) in item["severity_label"]


def test_invalid_utf8_is_explicit_and_does_not_break_json():
    hist = history()
    hist.append(event(text=b"a\xffb"), now=0.)
    item, = entries(hist)
    assert item["text"] == "a\ufffdb" and item["utf8_valid"] is False
    json.dumps(hist.snapshot(0.), allow_nan=False)


@pytest.mark.parametrize("change", [
    {"frame": b""}, {"frame": b"not-a-frame"}, {"fields": {}},
    {"sequence": True}, {"sequence": 11}, {"type_name": "ATTITUDE"},
    {"message_id": 30}, {"received_at": -1.}, {"received_at": 1.},
    {"received_at": float("nan")}, {"fields": {"severity": 6, "text": 42}},
    {"fields": {"severity": True, "text": "Received"}},
    {"fields": {"severity": 4, "text": "Received"}},
])
def test_malformed_events_cannot_replace_a_previous_valid_report(change):
    hist = history()
    good = event()
    hist.append(good, now=0.)
    before = entries(hist)
    assert not hist.append(replace(good, **change), now=0.)
    assert entries(hist) == before and hist.rejected == 1
    assert hist.last_rejection


def test_bad_standalone_chunk_number_and_inconsistent_extensions_are_rejected():
    hist = history()
    assert not hist.append(event(status_id=0, chunk=1), now=0.)
    good = event(status_id=3)
    assert not hist.append(replace(good, fields={**good.fields, "id": 4}), now=0.)
    assert hist.rejected == 2 and entries(hist) == []


def test_other_message_types_are_ignored_and_do_not_pollute_status_rejections():
    hist = history()
    other = replace(event(), message_id=0, type_name="HEARTBEAT")
    assert not hist.append(other, now=0.)
    assert hist.rejected == 0 and entries(hist) == []


@pytest.mark.parametrize("now", [True, "1", -1., float("inf"), float("nan")])
def test_invalid_call_clock_fails_before_any_mutation(now):
    hist = history()
    before = hist.snapshot(0.)
    with pytest.raises(ValueError):
        hist.append(event(), now=now)
    with pytest.raises(ValueError):
        hist.snapshot(now)
    with pytest.raises(ValueError):
        hist.reconnect("next", now)
    assert hist.snapshot(0.) == before


def test_delayed_receipt_keeps_its_original_time_and_snapshot_is_detached():
    hist = history()
    hist.append(event(1.), now=1.5)
    snap = hist.snapshot(2.)
    assert snap["entries"][0]["received_at"] == 1. and snap["entries"][0]["rx_age_s"] == 1.
    snap["entries"][0]["text"] = "changed"
    assert entries(hist, 2.)[0]["text"] == "Received"
    with pytest.raises(ValueError):
        hist.snapshot(1.9)
    assert not hist.append(event(.9), now=2.)


@pytest.mark.parametrize("options", [
    {"max_entries": 0}, {"max_entries": True}, {"max_entries": 201},
    {"max_pending": 0}, {"max_pending": 33}, {"max_entries": 1, "max_pending": 2},
    {"max_chunks": 0}, {"max_chunks": 257}, {"chunk_timeout_s": 0},
    {"chunk_timeout_s": float("inf")}, {"chunk_timeout_s": 61}, {"started_at": -1.},
])
def test_invalid_retention_policy_is_rejected(options):
    with pytest.raises(ValueError):
        history(**options)


@pytest.mark.parametrize("code", list(range(9)) + [255, None])
def test_system_status_reports_exact_heartbeat_declaration_and_reception_age(code):
    view = {"fields": None if code is None else {"system_status": code},
            "state": "absent" if code is None else "stale",
            "received_at": None if code is None else 2.,
            "rx_age_s": None if code is None else 10., "age_limit_s": 2.5}
    result = system_status_view(view)
    assert result["system_status"] == code
    assert result["state"] == ("unknown" if code is None else "stale")
    assert result["known"] == (code is not None and code < 9)
    assert result["received_at"] == view["received_at"] and result["rx_age_s"] == view["rx_age_s"]
    assert "healthy" not in result and "armed" not in result
    if code == 6:
        assert result["name"] == "MAV_STATE_EMERGENCY"
        assert result["label"] == "Urgence"


def test_unknown_wire_status_still_has_a_recent_local_reception():
    result = system_status_view({"fields": {"system_status": 255}, "state": "recent",
                                 "received_at": 0., "rx_age_s": .1, "age_limit_s": 1.})
    assert result["state"] == "recent" and not result["known"]
    assert result["label"] == "Inconnu (255)"


def test_http_session_contract_source_filter_and_reconnect_use_only_memory_receivers(tmp_path):
    """Real codec/session/GET serialization, no sockets, camera, service or journal."""
    import asyncio
    from collections import deque

    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from argos.backends.mavlink import MavlinkLink, SequenceScope
    from argos.console.app import create_app
    from argos.console.config import ConsoleConfig
    from argos.console.session import ConsoleSession

    class Input:
        datagram = False

        def __init__(self):
            self.chunks = deque()
            self.reads = 0
            self.closed = False

        def read(self):
            assert not self.closed
            self.reads += 1
            return self.chunks.popleft() if self.chunks else b""

        def write(self, _data):
            raise AssertionError("passive status inspection must not transmit")

        def close(self):
            self.closed = True

    inputs, now = [], [0.]

    def factory():
        source = Input()
        inputs.append(source)
        return MavlinkLink(source, sequence_scope=SequenceScope.COMPONENT)

    def heartbeat(status, *, component=1):
        encoder = mav.MAVLink(None, srcSystem=1, srcComponent=component)
        return mav.MAVLink_heartbeat_message(2, 3, 0, 0, status, 3).pack(encoder)

    config = ConsoleConfig(environment="simulation", mavlink_bind=("127.0.0.1", 0),
                           mavlink_peer=("127.0.0.1", 14550),
                           sequence_scope=SequenceScope.COMPONENT, recordings_dir=tmp_path)
    session = ConsoleSession(config, clock=lambda: now[0], link_factory=factory)
    session.start()
    client = TestClient(create_app(session=session))  # deliberately no background lifespan
    try:
        inputs[0].chunks.append(heartbeat(6) + heartbeat(4, component=2)
            + event(text=b"A" * 50, status_id=9).frame
            + event(text=b"foreign suffix", status_id=9, chunk=1, component=2).frame)
        session.tick()
        read_count = inputs[0].reads
        response = client.get("/api/state")
        assert response.status_code == 200 and inputs[0].reads == read_count
        current = response.json()["telemetry"]["autopilot_status"]
        assert set(current) == {"declaration", "texts"}
        assert current["declaration"] == {
            "state": "recent", "system_status": 6, "name": "MAV_STATE_EMERGENCY",
            "label": "Urgence", "known": True, "received_at": 0.,
            "rx_age_s": 0., "age_limit_s": config.limits.heartbeat,
        }
        item, = current["texts"]["entries"]
        assert item["reason"] == "assembling" and item["text"] == "A" * 50
        old_connection = item["connection_id"]

        now[0] = .5
        asyncio.run(session.reconnect("mavlink"))
        assert inputs[0].closed and len(inputs) == 2
        current = client.get("/api/state").json()["telemetry"]["autopilot_status"]
        assert current["declaration"]["state"] == "unknown"
        assert current["texts"]["connection_id"] != old_connection
        assert current["texts"]["entries"][0]["connection_id"] == old_connection
        assert current["texts"]["entries"][0]["reason"] == "reconnect"

        now[0] = .6
        inputs[1].chunks.append(event(.6, b"new suffix", status_id=9, chunk=1).frame)
        session.tick()
        current = client.get("/api/state").json()["telemetry"]["autopilot_status"]
        assert [item["reason"] for item in current["texts"]["entries"]] == ["missing_chunk", "reconnect"]

        # Full source changes use a new owner, so no old source's history carries over.
        replacement = ConsoleSession(replace(config, component=2), clock=lambda: now[0], link_factory=factory)
        replacement.start()
        try:
            texts = replacement.state()["telemetry"]["autopilot_status"]["texts"]
            assert texts["component"] == 2 and texts["entries"] == []
        finally:
            replacement.close()
        assert list(tmp_path.iterdir()) == []
    finally:
        client.close()
        session.close()
