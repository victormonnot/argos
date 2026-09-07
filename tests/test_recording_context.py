"""Capture provenance: version compatibility, strict bounds and complete integrity."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import io
import json
from types import MappingProxyType

import pytest

mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")

from argos.backends.mavlink import Recording, SequenceScope, TelemetryLimits
from argos.backends.mavlink.recording import (
    MAX_CONTEXT_BYTES, MAX_CONTEXT_DEPTH, MAX_CONTEXT_ITEMS, RecordingError,
    RecordingWriter, normalize_context, read_recording,
)
from argos.console.config import ConsoleConfig
from argos.console.context import capture_context, parse_capture_context
from argos.console.recording import ConsoleRecorder


RUN = "1234567890abcdef1234567890abcdef"
UTC = "2026-09-07T11:22:33.123456Z"


def line(record):
    return json.dumps(record, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"


def header(schema=2):
    return {"kind": "header", "format": "argos.mavlink.rx", "version": schema,
            "dialect": "ardupilotmega", "codec_version": version("pymavlink"),
            "clock": "local_receive", "started_at": 0.}


def finish_lines(records):
    prefix = b"".join(record if isinstance(record, bytes) else line(record) for record in records)
    return prefix + line({"kind": "checksum", "sha256": hashlib.sha256(prefix).hexdigest()})


def fixture(context=None, *, schema=2):
    records = [header(schema)]
    if schema == 2:
        records.append({"kind": "context", "data": {} if context is None else context})
    records.append({"kind": "end", "ended_at": 4., "events": 0})
    return finish_lines(records)


def config(**changes):
    result = ConsoleConfig(environment="simulation", video_source="gazebo", video_endpoint="/camera",
                           mavlink_bind=("127.0.0.1", 0), mavlink_peer=("127.0.0.1", 14550),
                           sequence_scope=SequenceScope.CHANNEL, video_age=1.3, battery_age=2.4,
                           limits=TelemetryLimits(2.5, .3, .6))
    return replace(result, **changes)


def console_context(**kwargs):
    return capture_context(config(), RUN, "UDP 127.0.0.1:15001 ← 127.0.0.1:14550",
                           captured_at_utc=UTC, **kwargs)


def test_default_writer_preserves_exact_version_one_bytes_and_positional_recording():
    stream = io.BytesIO()
    RecordingWriter(stream).finish(4.)
    assert stream.getvalue() == fixture(schema=1)
    assert read_recording(io.BytesIO(stream.getvalue())).context is None
    assert Recording(0., 4., "test", ()).context is None


def test_version_two_fixture_and_writer_have_identical_context_and_checksum_contract():
    context = {"format": "another.consumer", "version": 3, "notes": ["réception", None, True, 12, .5]}
    stream = io.BytesIO()
    RecordingWriter(stream, context=context).finish(4.)
    assert stream.getvalue() == fixture(context)
    recording = read_recording(io.BytesIO(fixture(context)))
    assert recording.context["notes"] == ("réception", None, True, 12, .5)
    assert recording.events == () and recording.ended_at == 4.
    with pytest.raises(TypeError):
        recording.context["version"] = 4


def test_context_is_frozen_recursively_and_writer_does_not_borrow_mutable_values():
    source = {"nested": {"list": [{"name": "original"}]}}
    stream = io.BytesIO()
    writer = RecordingWriter(stream, context=source)
    source["nested"]["list"][0]["name"] = "changed"
    writer.finish(0.)
    context = read_recording(io.BytesIO(stream.getvalue())).context
    assert context["nested"]["list"][0]["name"] == "original"
    with pytest.raises(TypeError):
        context["nested"]["list"][0]["name"] = "changed"
    assert normalize_context(MappingProxyType({"rows": (1, 2)})) == {"rows": [1, 2]}


@pytest.mark.parametrize("wire_v1", [False, True])
def test_version_two_preserves_rx_frames_timestamps_and_nonfinite_wire_payload(wire_v1):
    from argos.backends.mavlink import Received
    encoder = mav.MAVLink(None, srcSystem=3, srcComponent=42)
    encoder.seq = 250
    message = mav.MAVLink_attitude_message(17, float("nan"), .2, .3, 0., 0., 0.)
    frame = bytes(message.pack(encoder, force_mavlink1=wire_v1))
    event = Received(1.25, 3, 42, 250, 30, "ATTITUDE", message.to_dict(), frame)
    stream = io.BytesIO()
    writer = RecordingWriter(stream, context={"label": "original source"})
    writer.append(event)
    writer.finish(5.)
    independent = finish_lines([
        header(), {"kind": "context", "data": {"label": "original source"}},
        {"kind": "rx", "received_at": 1.25, "frame_hex": frame.hex()},
        {"kind": "end", "ended_at": 5., "events": 1},
    ])
    assert stream.getvalue() == independent
    recording = read_recording(io.BytesIO(independent))
    assert recording.events[0].frame == frame
    assert recording.events[0].received_at == 1.25 and recording.ended_at == 5.
    assert recording.events[0].fields["roll"] != recording.events[0].fields["roll"]
    assert recording.context["label"] == "original source"


@pytest.mark.parametrize("context", [
    [], "text", 1, True, {1: "nonstring key"}, {"value": object()},
    {"value": b"bytes"}, {"value": {1, 2}},
    {"value": float("nan")}, {"value": float("inf")}, {"value": -float("inf")},
    {"value": 10**5000}, {"value": "x" * MAX_CONTEXT_BYTES},
    {"rows": list(range(MAX_CONTEXT_ITEMS))},
])
def test_invalid_context_is_rejected_before_any_stream_write(context):
    stream = io.BytesIO()
    with pytest.raises(RecordingError):
        RecordingWriter(stream, context=context)
    assert stream.getvalue() == b""


def test_context_depth_and_cycles_are_bounded_without_recursion_failure():
    nested = {"value": 1}
    for _ in range(MAX_CONTEXT_DEPTH + 1):
        nested = {"nested": nested}
    cyclic = {}
    cyclic["self"] = cyclic
    for context in (nested, cyclic):
        stream = io.BytesIO()
        with pytest.raises(RecordingError, match="nesting"):
            RecordingWriter(stream, context=context)
        assert stream.getvalue() == b""


def test_only_context_line_has_the_larger_inclusive_bound():
    context_line = line({"kind": "context", "data": {"note": "x" * 2000}})
    context_line = context_line[:-1].ljust(MAX_CONTEXT_BYTES - 1, b" ") + b"\n"
    records = [header(), context_line, {"kind": "end", "ended_at": 4., "events": 0}]

    class BoundedReader(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.bounds = []

        def readline(self, size=-1):
            self.bounds.append(size)
            assert size in (1025, MAX_CONTEXT_BYTES + 1)
            return super().readline(size)

    stream = BoundedReader(finish_lines(records))
    assert len(read_recording(stream).context["note"]) == 2000
    assert stream.bounds == [1025, MAX_CONTEXT_BYTES + 1, 1025, 1025]
    records[1] = context_line[:-1] + b" \n"
    with pytest.raises(RecordingError):
        read_recording(BoundedReader(finish_lines(records)))
    for index in (0, 2):
        records = [line(header()), line({"kind": "context", "data": {}}),
                   line({"kind": "end", "ended_at": 4., "events": 0})]
        records[index] = records[index][:-1].ljust(1024, b" ") + b"\n"
        with pytest.raises(RecordingError):
            read_recording(BoundedReader(finish_lines(records)))


def test_writer_context_byte_limit_is_inclusive_and_counts_json_escaping():
    available = MAX_CONTEXT_BYTES - len(line({"kind": "context", "data": {"note": ""}}))
    stream = io.BytesIO()
    writer = RecordingWriter(stream, context={"note": "x" * available})
    writer.finish(4.)
    assert len(stream.getvalue().splitlines(keepends=True)[1]) == MAX_CONTEXT_BYTES
    assert len(read_recording(io.BytesIO(stream.getvalue())).context["note"]) == available
    for note in ("x" * (available + 1), "é" * (available // 6 + 1)):
        stream = io.BytesIO()
        with pytest.raises(RecordingError):
            RecordingWriter(stream, context={"note": note})
        assert stream.getvalue() == b""


def test_reader_still_applies_rx_line_bound_and_event_limit_in_version_two():
    encoder = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    frame = bytes(mav.MAVLink_heartbeat_message(6, 8, 0, 0, 0, 3).pack(encoder))
    rx = line({"kind": "rx", "received_at": 1., "frame_hex": frame.hex()})
    records = [header(), {"kind": "context", "data": {}}, rx,
               {"kind": "end", "ended_at": 4., "events": 1}]
    with pytest.raises(RecordingError, match="max_events"):
        read_recording(io.BytesIO(finish_lines(records)), max_events=0)
    records[2] = rx[:-1].ljust(1024, b" ") + b"\n"
    with pytest.raises(RecordingError, match="oversized"):
        read_recording(io.BytesIO(finish_lines(records)))


def test_reader_rejects_missing_duplicate_late_and_version_one_context():
    context = {"kind": "context", "data": {}}
    end = {"kind": "end", "ended_at": 4., "events": 0}
    cases = ([header(), end], [header(), context, context, end],
             [header(1), context, end], [header(), end, context],
             [header(), {"kind": "context", "data": {}, "extra": 1}, end],
             [header(), {"kind": "context"}, end])
    for records in cases:
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(finish_lines(records)))


@pytest.mark.parametrize("raw", [
    b'{"kind":"context","data":{"duplicate":1,"duplicate":2}}\n',
    b'{"kind":"context","data":{"v":NaN}}\n',
    b'{"kind":"context","data":{"v":Infinity}}\n',
    b'{"kind":"context","data":{"v":1e999}}\n',
    b'{"kind":"context","data":null}\n',
    b'{"kind":"context","data":[]}\n',
])
def test_reader_rejects_invalid_context_even_when_checksum_matches(raw):
    records = [header(), raw, {"kind": "end", "ended_at": 4., "events": 0}]
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(finish_lines(records)))


def test_reader_checks_context_digest_and_never_returns_a_truncated_prefix():
    complete = fixture({"label": "original"})
    with pytest.raises(RecordingError, match="SHA-256"):
        read_recording(io.BytesIO(complete.replace(b"original", b"modified")))
    for end in range(len(complete)):
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(complete[:end]))
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(complete + b"\n"))


def test_context_write_failure_never_produces_a_success_footer():
    class FailingContext(io.BytesIO):
        writes = 0

        def write(self, data):
            self.writes += 1
            if self.writes == 2:
                super().write(data[:4])
                return 4
            return super().write(data)

    stream = FailingContext()
    with pytest.raises(RecordingError):
        RecordingWriter(stream, context={"label": "capture"})
    assert stream.writes == 2 and b'"checksum"' not in stream.getvalue()
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(stream.getvalue()))


def test_console_context_preserves_declared_configuration_resolved_endpoint_and_limits():
    result = console_context()
    assert result == {"format": "argos.console.capture", "version": 1,
                      "captured_at_utc": UTC, "run_id": RUN,
                      "configuration": config().public(),
                      "telemetry_endpoint": "UDP 127.0.0.1:15001 ← 127.0.0.1:14550",
                      "age_limits_s": {"video": 1.3, "heartbeat": 2.5, "battery": 2.4,
                                       "attitude": .3, "local_position_ned": .6}}
    assert result["configuration"]["mavlink_bind"] == "127.0.0.1:0"
    assert "recordings_dir" not in result["configuration"]
    assert "gazebo_python_path" not in result["configuration"]
    parsed = parse_capture_context(read_recording(io.BytesIO(fixture(result))).context)
    parsed["configuration"]["component"] = 42
    assert result["configuration"]["component"] == 1


def test_console_context_date_is_utc_and_parser_accepts_no_implicit_timezone():
    utc = datetime(2026, 9, 7, 13, 22, 33, 123456, tzinfo=timezone(timedelta(hours=2)))
    result = capture_context(config(), RUN, config().telemetry_endpoint, captured_at_utc=utc)
    assert result["captured_at_utc"] == UTC
    current = capture_context(config(), RUN, config().telemetry_endpoint)
    assert current["captured_at_utc"].endswith("Z")
    assert datetime.fromisoformat(current["captured_at_utc"]).utcoffset() == timedelta(0)
    for stamp in ("2026-09-07", "2026-09-07T11:22:33", "2026-99-07T11:22:33Z",
                  "2026-09-07T13:22:33+02:00", None, True):
        malformed = {**result, "captured_at_utc": stamp}
        with pytest.raises(ValueError):
            parse_capture_context(malformed)


@pytest.mark.parametrize("limits", [
    {}, {"video": 1.}, {"video": True, "heartbeat": 1., "battery": 1., "attitude": 1., "local_position_ned": 1.},
    {"video": -1., "heartbeat": 1., "battery": 1., "attitude": 1., "local_position_ned": 1.},
    {"video": 0., "heartbeat": 1., "battery": 1., "attitude": 1., "local_position_ned": 1.},
])
def test_known_context_rejects_invalid_receipt_thresholds(limits):
    with pytest.raises(ValueError):
        parse_capture_context({**console_context(), "age_limits_s": limits})


@pytest.mark.parametrize("changes", [
    {"run_id": "not-a-run"}, {"version": True}, {"extra": 1},
    {"configuration": {}}, {"telemetry_endpoint": "UDP 127.0.0.1:15001 ← 127.0.0.1:9999"},
    {"telemetry_endpoint": "UDP 127.0.0.2:15001 ← 127.0.0.1:14550"},
    {"telemetry_endpoint": "UDP 127.0.0.1:70000 ← 127.0.0.1:14550"},
])
def test_known_context_rejects_incoherent_identity_or_source_labels(changes):
    with pytest.raises(ValueError):
        parse_capture_context({**console_context(), **changes})


@pytest.mark.parametrize("name", list(config().public()))
@pytest.mark.parametrize("malformed", [[], {"not": "a scalar"}, True])
def test_wrong_configuration_field_types_have_stable_validation_errors(name, malformed):
    context = console_context()
    context["configuration"][name] = malformed
    with pytest.raises(ValueError):
        parse_capture_context(context)


def test_unknown_context_is_not_interpreted_as_console_provenance():
    for value in (None, {}, [], {"format": "unknown", "version": 1},
                  {"format": "argos.console.capture", "version": 2}):
        assert parse_capture_context(value) is None


def test_tcp_serial_and_unconfigured_endpoint_consistency():
    for source in (ConsoleConfig(),
                   ConsoleConfig(environment="simulation", mavlink_tcp=("127.0.0.1", 5760), sequence_scope=SequenceScope.CHANNEL),
                   ConsoleConfig(environment="real", mavlink_device="/dev/ttyUSB0", sequence_scope=SequenceScope.COMPONENT)):
        result = capture_context(source, RUN, source.telemetry_endpoint, captured_at_utc=UTC)
        assert result["telemetry_endpoint"] == source.telemetry_endpoint
        with pytest.raises(ValueError):
            parse_capture_context({**result, "telemetry_endpoint": "unrelated endpoint"})


def test_console_recorder_uses_v3_and_preserves_supplied_context(tmp_path):
    recorder = ConsoleRecorder(tmp_path)
    for context in (None, console_context()):
        started = recorder.start(1., context=context)
        assert started["state"] == "recording"
        complete = recorder.stop(2.)
        assert complete["state"] == "complete"
        with recorder.completed_path(complete["id"]).open("rb") as stream:
            recording = read_recording(stream)
        assert recording.end_reason == "stopped" and recording.end_detail == ""
        if context is None:
            assert dict(recording.context) == {}
        if context is not None:
            assert parse_capture_context(recording.context) == context
