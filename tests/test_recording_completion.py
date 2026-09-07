"""Closure provenance is integrity protected and cannot conceal a failed write."""
import io

import pytest

pytest.importorskip("pymavlink")

from argos.backends.mavlink.recording import RecordingError, RecordingWriter, read_recording
from test_recording_context import header, finish_lines
from test_mavlink_recording import received, rx


def test_v3_wire_contract_and_interruption_survive_reading():
    event = received(1.)
    stream = io.BytesIO()
    writer = RecordingWriter(stream, context={"origin": "test"}, with_completion=True)
    writer.append(event)
    writer.finish(3., reason="transport_error", detail="Liaison interrompue : déconnexion")
    expected = finish_lines([
        header(3), {"kind": "context", "data": {"origin": "test"}}, rx(event),
        {"kind": "end", "ended_at": 3., "events": 1, "reason": "transport_error",
         "detail": "Liaison interrompue : déconnexion"},
    ])
    assert stream.getvalue() == expected and writer.bytes_written == len(expected)
    result = read_recording(io.BytesIO(expected))
    assert result.ended_at == 3. and result.events[0].frame == event.frame
    assert result.end_reason == "transport_error" and "déconnexion" in result.end_detail


def test_v3_closure_reason_and_detail_are_covered_by_checksum():
    stream = io.BytesIO()
    RecordingWriter(stream, with_completion=True).finish(1., reason="shutdown", detail="console stopped")
    for original, replacement in ((b'shutdown', b'stopped'), (b'console stopped', b'user stopped')):
        changed = stream.getvalue().replace(original, replacement)
        with pytest.raises(RecordingError, match="SHA-256"):
            read_recording(io.BytesIO(changed))


@pytest.mark.parametrize("change", [
    {"reason": None}, {"reason": True}, {"reason": "mission_success"}, {"detail": []},
    {"detail": "é" * 100}, {"detail": "failure\ud800"}, {"extra": 1},
])
def test_v3_rejects_bad_completion_even_with_valid_checksum(change):
    end = {"kind": "end", "ended_at": 1., "events": 0, "reason": "stopped", "detail": ""}
    end.update(change)
    data = finish_lines([header(3), {"kind": "context", "data": {}}, end])
    with pytest.raises(RecordingError):
        read_recording(io.BytesIO(data))


def test_versions_cannot_silently_drop_or_add_closure_metadata():
    for schema in (1, 2, 3):
        records = [header(schema)]
        if schema != 1:
            records.append({"kind": "context", "data": {}})
        end = {"kind": "end", "ended_at": 1., "events": 0}
        if schema != 3:
            end.update(reason="shutdown", detail="")
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(finish_lines([*records, end])))


def test_invalid_completion_does_not_write_and_writer_can_still_close():
    stream = io.BytesIO()
    writer = RecordingWriter(stream, with_completion=True)
    before = stream.getvalue()
    with pytest.raises(RecordingError):
        writer.finish(1., reason="unknown")
    with pytest.raises(RecordingError, match="Unicode"):
        writer.finish(1., detail="failure\ud800")
    assert stream.getvalue() == before
    writer.finish(1., reason="stopped")
    assert read_recording(io.BytesIO(stream.getvalue())).end_reason == "stopped"
    old = RecordingWriter(io.BytesIO())
    with pytest.raises(RecordingError):
        old.finish(1., reason="transport_error")


def test_version_three_requires_a_complete_context_and_footer():
    stream = io.BytesIO()
    RecordingWriter(stream, with_completion=True).finish(1.)
    data = stream.getvalue()
    lines = data.splitlines(keepends=True)
    for broken in (b"".join(lines[:1] + lines[2:]), b"".join(lines[:-1]), data[:-5], data + b" "):
        with pytest.raises(RecordingError):
            read_recording(io.BytesIO(broken))
