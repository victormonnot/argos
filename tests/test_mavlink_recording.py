"""Passive recordings: preserved bytes, complete validation and explicit failure.

Fixtures only contain generic telemetry. No transport or vehicle is opened.
Reader fixtures are built independently of RecordingWriter.
"""
from dataclasses import FrozenInstanceError, replace
import hashlib
from importlib.metadata import version
import io
import json
import math
import subprocess
import sys

import pytest

mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")

from argos.backends.mavlink import (
    Received, ReceptionState, TelemetryCache, TelemetryLimits, UpdateStatus,
)
from argos.backends.mavlink.recording import (
    RecordingError, RecordingWriter, read_recording,
)


def received(at=1., sequence=7, *, v1=False, message=None):
    encoder = mav.MAVLink(None, srcSystem=3, srcComponent=42)
    encoder.seq = sequence
    message = message or mav.MAVLink_heartbeat_message(6, 8, 0, 0, 0, 3)
    raw = bytes(message.pack(encoder, force_mavlink1=v1))
    decoded, = mav.MAVLink(None).parse_buffer(raw)
    return Received(at, decoded.get_srcSystem(), decoded.get_srcComponent(),
                    decoded.get_seq(), decoded.get_msgId(), decoded.get_type(),
                    decoded.to_dict(), raw)


def json_line(record):
    return json.dumps(record, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def header(started_at=0.):
    return {"kind": "header", "format": "argos.mavlink.rx", "version": 1,
            "dialect": "ardupilotmega", "codec_version": version("pymavlink"),
            "clock": "local_receive", "started_at": started_at}


def rx(event):
    return {"kind": "rx", "received_at": event.received_at,
            "frame_hex": event.frame.hex()}


def document(events=(), *, started_at=0., ended_at=4., head=None,
             event_records=None, end_updates=None, checksum_updates=None):
    records = [rx(event) for event in events] if event_records is None else event_records
    prefix = json_line(header(started_at) if head is None else head)
    prefix += b"".join(json_line(record) for record in records)
    end = {"kind": "end", "ended_at": ended_at, "events": len(records)}
    end.update(end_updates or {})
    prefix += json_line(end)
    checksum = {"kind": "checksum", "sha256": hashlib.sha256(prefix).hexdigest()}
    checksum.update(checksum_updates or {})
    return prefix + json_line(checksum)


@pytest.mark.parametrize("v1", [False, True])
def test_writer_preserves_frames_and_reader_redetects_fields(v1):
    stream = io.BytesIO()
    writer = RecordingWriter(stream, started_at=.5)
    first = received(.5, 254, v1=v1)
    # fields are explicitly outside the stored contract: raw bytes are authoritative.
    writer.append(replace(first, fields={"arbitrary": object()}))
    writer.append(received(.5, 0, v1=v1))
    writer.finish(6.)
    lines = stream.getvalue().splitlines(keepends=True)
    assert json.loads(lines[0]) == header(.5)
    assert json.loads(lines[1]) == rx(first)
    assert json.loads(lines[-2]) == {"kind": "end", "ended_at": 6., "events": 2}
    assert json.loads(lines[-1]) == {
        "kind": "checksum",
        "sha256": hashlib.sha256(b"".join(lines[:-1])).hexdigest(),
    }
    recording = read_recording(io.BytesIO(stream.getvalue()))
    assert (recording.started_at, recording.ended_at) == (.5, 6.)
    assert recording.codec_version == version("pymavlink")
    assert isinstance(recording.events, tuple)
    assert [event.sequence for event in recording.events] == [254, 0]
    replayed = recording.events[0]
    assert replayed.frame == first.frame and replayed.received_at == .5
    assert dict(replayed.fields) == first.fields
    with pytest.raises(TypeError):
        replayed.fields["type"] = 99
    with pytest.raises(FrozenInstanceError):
        recording.ended_at = 7.


def test_reader_accepts_empty_recording_and_equal_end_time():
    recording = read_recording(io.BytesIO(document(started_at=2., ended_at=2.)),
                               max_events=0)
    assert recording.events == ()
    assert recording.started_at == recording.ended_at == 2.


def test_old_codec_version_is_exposed_instead_of_silently_replaced():
    head = header()
    head["codec_version"] = "older-codec-example"
    recording = read_recording(io.BytesIO(document([received()], head=head)))
    assert recording.codec_version == "older-codec-example"


def test_footer_hash_covers_original_bytes_including_json_whitespace():
    original = document([received()])
    lines = original.splitlines(keepends=True)
    # Legal but noncanonical JSON serialization must still be hashed verbatim.
    lines[0] = json.dumps(header(), indent=None).encode() + b"  \n"
    end = json.loads(lines[-1])
    end["sha256"] = hashlib.sha256(b"".join(lines[:-1])).hexdigest()
    lines[-1] = json_line(end)
    assert len(read_recording(io.BytesIO(b"".join(lines))).events) == 1
    lines[0] = lines[0][:-1] + b" \n"
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(b"".join(lines)))


def test_wire_nan_is_preserved_then_rejected_by_telemetry_cache():
    event = received(1., message=mav.MAVLink_attitude_message(
        15, math.nan, .2, .3, 0., 0., 0.))
    stream = io.BytesIO()
    writer = RecordingWriter(stream)
    writer.append(event)
    writer.finish(4.)
    assert b"NaN" not in stream.getvalue()
    recording = read_recording(io.BytesIO(stream.getvalue()))
    replayed, = recording.events
    assert replayed.frame == event.frame and math.isnan(replayed.fields["roll"])
    cache = TelemetryCache(system=3, component=42,
                           limits=TelemetryLimits(1., 1., 1.))
    assert cache.update(replayed, replayed.received_at).status is UpdateStatus.REJECTED
    final = cache.snapshot(recording.ended_at)
    assert final.attitude.state is ReceptionState.ABSENT and final.rejected == 1


def test_final_silence_keeps_original_reception_and_produces_stale_snapshot():
    recording = read_recording(io.BytesIO(document([received(1.)], ended_at=9.)))
    cache = TelemetryCache(system=3, component=42,
                           limits=TelemetryLimits(2., 2., 2.))
    for event in recording.events:
        assert cache.update(event, event.received_at).accepted
    final = cache.snapshot(recording.ended_at)
    assert final.heartbeat.message.received_at == 1.
    assert final.heartbeat.rx_age == 8.
    assert final.heartbeat.state is ReceptionState.STALE


@pytest.mark.parametrize("changes", [
    {"system": 4}, {"component": 41}, {"sequence": 8},
    {"message_id": 30}, {"type_name": "ATTITUDE"},
    {"system": True}, {"sequence": 7.},
])
def test_writer_rejects_identity_mismatch_before_writing_and_can_recover(changes):
    stream = io.BytesIO()
    writer = RecordingWriter(stream)
    before = stream.getvalue()
    with pytest.raises(RecordingError):
        writer.append(replace(received(10.), **changes))
    assert stream.getvalue() == before
    writer.append(received(1.))
    writer.finish(1.)
    assert len(read_recording(io.BytesIO(stream.getvalue())).events) == 1


@pytest.mark.parametrize("bad", [True, None, "1", -1., math.nan, math.inf, 10**1000])
def test_invalid_writer_dates_do_not_write_or_advance_clock(bad):
    stream = io.BytesIO()
    with pytest.raises(RecordingError):
        RecordingWriter(stream, started_at=bad)
    assert stream.getvalue() == b""
    writer = RecordingWriter(stream)
    before = stream.getvalue()
    with pytest.raises(RecordingError):
        writer.append(replace(received(), received_at=bad))
    assert stream.getvalue() == before
    writer.append(received(1.))
    before = stream.getvalue()
    with pytest.raises(RecordingError):
        writer.finish(bad)
    assert stream.getvalue() == before
    writer.finish(1.)


def test_backwards_append_or_finish_is_recoverable_and_equal_dates_are_valid():
    stream = io.BytesIO()
    writer = RecordingWriter(stream, started_at=2.)
    for event in [received(1.), received(2.), received(3.), received(2.5)]:
        if event.received_at in (1., 2.5):
            before = stream.getvalue()
            with pytest.raises(RecordingError):
                writer.append(event)
            assert stream.getvalue() == before
        else:
            writer.append(event)
    with pytest.raises(RecordingError):
        writer.finish(2.)
    writer.append(received(3.))
    writer.finish(3.)
    assert [event.received_at for event in read_recording(
        io.BytesIO(stream.getvalue())).events] == [2., 3., 3.]


def test_writer_rejects_non_event_before_mutation():
    stream = io.BytesIO()
    writer = RecordingWriter(stream)
    before = stream.getvalue()
    with pytest.raises(RecordingError):
        writer.append(None)
    assert stream.getvalue() == before
    writer.finish(0.)


class FailingWriter(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.failure = "disabled"
        self.failure_at = None
        self.attempts = 0
        self.flushes = 0

    def write(self, data):
        self.attempts += 1
        if self.failure == "disabled" or (self.failure_at is not None
                                          and self.attempts != self.failure_at):
            return super().write(data)
        if isinstance(self.failure, Exception):
            raise self.failure
        if type(self.failure) is int and 0 <= self.failure < len(data):
            super().write(data[:self.failure])
        return self.failure

    def flush(self):
        self.flushes += 1
        return super().flush()


@pytest.mark.parametrize("failure", [0, 5, None, -1, True, 10000,
                                     OSError("broken"), BlockingIOError()])
def test_write_failure_is_fatal_without_followup_writes_or_footer(failure):
    stream = FailingWriter()
    writer = RecordingWriter(stream)
    stream.failure = failure
    with pytest.raises(RecordingError):
        writer.append(received())
    attempts = stream.attempts
    incomplete = stream.getvalue()
    stream.failure = "disabled"
    with pytest.raises(RecordingError):
        writer.append(received(2.))
    with pytest.raises(RecordingError):
        writer.finish(2.)
    assert stream.attempts == attempts and stream.getvalue() == incomplete
    assert not stream.closed and stream.flushes == 0
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(incomplete))


def test_finish_is_explicit_single_use_and_does_not_own_the_stream():
    stream = FailingWriter()
    writer = RecordingWriter(stream)
    writer.append(received())
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(stream.getvalue()))
    writer.finish(1.)
    complete = stream.getvalue()
    with pytest.raises(RecordingError):
        writer.finish(1.)
    with pytest.raises(RecordingError):
        writer.append(received(1.))
    assert stream.getvalue() == complete
    assert not stream.closed and stream.flushes == 0


@pytest.mark.parametrize("write_number", [1, 2], ids=["end", "checksum"])
def test_failure_while_writing_footer_prevents_retry(write_number):
    stream = FailingWriter()
    writer = RecordingWriter(stream)
    writer.append(received())
    stream.failure = 4
    stream.failure_at = stream.attempts + write_number
    with pytest.raises(RecordingError):
        writer.finish(2.)
    attempts = stream.attempts
    stream.failure = "disabled"
    with pytest.raises(RecordingError):
        writer.finish(2.)
    assert stream.attempts == attempts
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(stream.getvalue()))


def test_every_truncated_prefix_and_every_suffix_is_rejected():
    complete = document([received()])
    for cut in range(len(complete)):
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(complete[:cut]))
    for suffix in (b"\n", b" ", b"garbage", complete, complete.splitlines(keepends=True)[-1]):
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(complete + suffix))


@pytest.mark.parametrize("updates", [
    {"events": 0}, {"events": True}, {"events": 1.},
    {"ended_at": .5}, {"ended_at": True}, {"kind": "rx"},
])
def test_reader_rejects_incoherent_footer(updates):
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(document([received()], end_updates=updates)))


def test_checksum_is_required_and_rejects_invalid_digest():
    for updates in ({"sha256": "0" * 64}, {"sha256": "z" * 64},
                    {"kind": "end"}):
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(document([received()], checksum_updates=updates)))


def test_tampering_with_end_time_or_count_is_detected_by_checksum():
    lines = document([received()], ended_at=4.).splitlines(keepends=True)
    for updates in ({"ended_at": 40.}, {"events": 10}):
        modified = list(lines)
        end = json.loads(modified[-2])
        end.update(updates)
        modified[-2] = json_line(end)
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(b"".join(modified)))


@pytest.mark.parametrize("updates", [
    {"version": True}, {"version": 1.}, {"version": 2},
    {"format": "another-format"}, {"dialect": "common"},
    {"clock": "firmware"}, {"kind": "rx"}, {"started_at": -1.},
])
def test_reader_rejects_wrong_header_contract(updates):
    head = header()
    head.update(updates)
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(document([received()], head=head)))


def test_reader_rejects_duplicate_keys_missing_keys_unknown_keys_and_blank_lines():
    valid = document([received()])
    lines = valid.splitlines(keepends=True)
    for index in range(4):
        parsed = json.loads(lines[index])
        key = next(iter(parsed))
        duplicate = lines[index][:-2] + b"," + json.dumps(key).encode() + b":" + \
            json.dumps(parsed[key]).encode() + b"}\n"
        missing = dict(parsed)
        del missing[key]
        unknown = {**parsed, "unexpected": 0}
        for changed in (duplicate, json_line(missing), json_line(unknown), b"\n"):
            altered = list(lines)
            altered[index] = changed
            if index != 3:
                end = json.loads(altered[-1])
                end["sha256"] = hashlib.sha256(b"".join(altered[:-1])).hexdigest()
                altered[-1] = json_line(end)
            with pytest.raises(RecordingError):
                read_recording(io.BytesIO(b"".join(altered)))


def test_nonfinite_json_dates_are_rejected_even_with_valid_checksum():
    valid = document([received()])
    lines = valid.splitlines(keepends=True)
    for index, key in ((0, "started_at"), (1, "received_at"), (2, "ended_at")):
        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            record = json.loads(lines[index])
            record[key] = "REPLACE_TIME"
            altered = list(lines)
            altered[index] = json_line(record).replace(b'"REPLACE_TIME"', token)
            checksum = json.loads(altered[-1])
            checksum["sha256"] = hashlib.sha256(b"".join(altered[:-1])).hexdigest()
            altered[-1] = json_line(checksum)
            with pytest.raises(RecordingError):
                read_recording(io.BytesIO(b"".join(altered)))


def test_reader_rejects_receipts_before_origin_and_backwards_dates():
    for events, started in (([received(.5)], 1.),
                            ([received(2.), received(1.)], 0.)):
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(document(events, started_at=started)))


def invalid_frames():
    raw = received().frame
    corrupt = bytearray(raw)
    corrupt[-1] ^= 0xff
    unknown = bytearray(raw)
    unknown[7:10] = b"\xff\xff\xff"
    return [b"", b"x" * 7, b"x" * 281, bytes(corrupt), bytes(unknown),
            b"noise" + raw, raw + b"noise", raw + raw, raw[:-1], raw + raw[:8]]


def test_invalid_frames_are_rejected_on_write_and_read_without_silent_salvage():
    for raw in invalid_frames():
        stream = io.BytesIO()
        writer = RecordingWriter(stream)
        before = stream.getvalue()
        with pytest.raises(RecordingError):
            writer.append(replace(received(10.), frame=raw))
        assert stream.getvalue() == before
        writer.append(received(1.))
        writer.finish(1.)
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(document(event_records=[{
                "kind": "rx", "received_at": 1., "frame_hex": raw.hex(),
            }])))


def test_frame_hex_must_be_a_canonical_lowercase_string():
    value = received().frame.hex()
    for invalid in (value.upper(), value[:4] + " " + value[4:], value[:-1],
                    "gg" + value[2:], None, 42):
        record = rx(received())
        record["frame_hex"] = invalid
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(document(event_records=[record])))


def test_line_length_limit_is_inclusive_and_checked_before_unbounded_read():
    parts = document().splitlines(keepends=True)
    parts[0] = parts[0][:-1].ljust(1023, b" ") + b"\n"
    end = json.loads(parts[-1])
    end["sha256"] = hashlib.sha256(b"".join(parts[:-1])).hexdigest()
    parts[-1] = json_line(end)
    assert read_recording(io.BytesIO(b"".join(parts))).events == ()

    class BoundedReader(io.BytesIO):
        def readline(self, size=-1):
            assert 0 < size <= 1025, "line reads must have a finite bound"
            return super().readline(size)

    for oversized in (parts[0][:-1] + b" \n", b"x" * 100_000):
        with pytest.raises(RecordingError):
            read_recording(BoundedReader(oversized))


@pytest.mark.parametrize("bad", [True, -1, 1., None])
def test_event_limit_rejects_invalid_configuration(bad):
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(document()), max_events=bad)


def test_event_limit_fails_instead_of_returning_a_validated_prefix():
    data = document([received(1.), received(2.)])
    assert len(read_recording(io.BytesIO(data), max_events=2).events) == 2
    for limit in (0, 1):
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(data), max_events=limit)


def test_importing_recording_does_not_load_optional_codec_or_serial():
    code = (
        "import sys; import argos.backends.mavlink.recording; "
        "assert not any(k == 'pymavlink' or k.startswith('pymavlink.') "
        "for k in sys.modules); assert 'serial' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)
