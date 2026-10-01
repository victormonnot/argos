"""A battery capture shares existing camera/radio owners and survives normal lifecycle."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.session import ConsoleSession
from argos.console.video import DeviceCamera, VideoStore
from argos.console.vision import AnalyzedFrame
from argos.console.yaw_assist import YawAssistService


ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    now = [10.]
    config = ConsoleConfig(environment="real", video_source="device", video_endpoint="/dev/video99",
                           recordings_dir=tmp_path, yaw_assist=True, vision_model=Path("fixture.onnx"))
    session = ConsoleSession(config, clock=lambda: now[0])
    session._started = True
    radio = YawAssistService("/dev/nonexistent", 8080, clock=lambda: now[0] + 10000.,
                             manifest={"runtime_sha256": "fixture"})
    def forbidden(*args, **kwargs):
        pytest.fail("Filming must not open another source or send a flight command")
    monkeypatch.setattr(DeviceCamera, "start", forbidden)
    monkeypatch.setattr(session.control, "_send", forbidden)
    monkeypatch.setattr(radio, "start", forbidden)
    client = TestClient(create_app(session=session, yaw_service=radio))
    session.vision._context = session.run_id, session.video_source_id
    def frame(at):
        now[0] = at
        session.video.accept_raw(width=8, height=6, step=24, pixel_format="RGB_INT8",
                                 data=bytes([45, 80, 90]) * 48, received_at=at)
        return session.video.latest(at)
    frame(10.)
    yield SimpleNamespace(session=session, client=client, radio=radio, now=now, frame=frame)
    session.close()
    session.recorder.visual.wait(3.)
    if session.recorder.filming is not None:
        session.recorder.filming.stop(timeout=3.)
    radio.close()
    client.close()


def start(rig, **values):
    return rig.client.post("/api/recordings/start", headers=ORIGIN,
                           json={"include_visual": True, **values})


def radio_status(at):
    return {"connected": True, "session": "abcd1234", "generation": 1,
        "radio_state": "A", "radio_mode": "D", "radio_yaw_phase": "M", "radio_pitch_phase": "A",
        "pilot_sample": {"schema_version": 1, "received_at": at,
            "session": "abcd1234", "generation": 1, "state": "A", "mode": "D",
            "yaw_phase": "M", "pitch_phase": "A", "sticks": {"roll": 0, "pitch": 0, "yaw": 500, "throttle": -900},
            "lua_outputs": {"yaw": {"valid": False, "value": 0}, "pitch": {"valid": True, "value": 35}}},
        "last_sent_command": {"sequence": 1, "pitch": {"valid": True, "value": 35}}}


def test_default_assist_capture_keeps_camera_cadence_and_synchronized_sticks(rig):
    response = start(rig, label="Battery 1")
    assert response.status_code == 200, response.text
    capture = rig.session.recorder.filming
    identifier = response.json()["id"]
    for index in range(1, 31):
        sample = rig.frame(10. + index / 30.)
        if index % 3 == 0:
            rig.session.vision._frame = AnalyzedFrame(sample,
                (rig.session.run_id, rig.session.video_source_id),
                {"width": 8, "height": 6, "inference_ms": 30., "detections": []})
            rig.session.tick()
            rig.radio._publish(radio_status(rig.radio.clock()))
    result = rig.client.post("/api/recordings/stop", headers=ORIGIN, json={})
    assert result.status_code == 200
    done = capture.stop(timeout=3.)
    assert done["writer_stopped"] and done["complete"], done
    assert done["camera"]["written_frames"] == 30
    assert done["camera"]["observed_fps"] == pytest.approx(30.)
    assert done["pilot_samples"] == 10
    rows = [json.loads(row) for row in (capture.directory / "events.jsonl").read_text().splitlines()]
    radio = [row for row in rows if row["kind"] == "radio"]
    vision = [row for row in rows if row["kind"] == "vision"]
    assert len(radio) == 10 and len(vision) == 10
    assert all(row["status"]["assistance"]["yaw"]["state"] == "manual" for row in radio)
    assert all(row["status"]["assistance"]["pitch"]["state"] == "assisted" for row in radio)
    anchor = radio[0]["clock_anchor"]
    receipt = radio[0]["status"]["pilot_sample"]["received_at"]
    console_receipt = receipt - anchor["host_monotonic_at"] + anchor["console_before"]
    assert console_receipt == pytest.approx(10.1)
    frames = [json.loads(row) for row in (capture.directory / "camera.frames.jsonl").read_text().splitlines()]
    assert frames[2]["received_at"] == pytest.approx(console_receipt)
    assert frames[2]["source_id"] == vision[0]["video_id"] == rig.session.video_source_id
    assert frames[2]["sequence"] == vision[0]["frame_sequence"]
    assert all(row["recording_id"] == identifier for row in rows)
    assert (capture.directory.parent / f"{identifier}.jsonl").exists()
    assert (capture.directory / "manifest.json").is_file()
    before = done["events"]
    rig.radio._publish(radio_status(rig.radio.clock()))
    rig.frame(11.1)
    assert capture.status()["events"] == before
    assert capture.status()["camera"]["written_frames"] == 30


def test_capture_opt_out_and_passive_video_default_preserve_old_format(rig):
    assert start(rig, include_filming=False).status_code == 200
    assert rig.session.recorder.filming is None
    rig.client.post("/api/recordings/stop", headers=ORIGIN, json={})
    rig.session.recorder.visual.wait(3.)
    rig.session.config = replace(rig.session.config, yaw_assist=False)
    assert start(rig).status_code == 200
    assert rig.session.recorder.filming is None


@pytest.mark.parametrize("values", [
    {"include_filming": 1}, {"label": []}, {"label": "x" * 81}, {"label": "bad\nlabel"},
    {"include_filming": True, "include_visual": False}, {"unknown": True},
])
def test_invalid_capture_options_do_not_start_a_recording(rig, values):
    assert start(rig, **values).status_code == 422
    assert not rig.session.recorder.active


def test_camera_replacement_keeps_same_take_and_records_distinct_source(rig):
    assert start(rig).status_code == 200
    capture = rig.session.recorder.filming
    original = rig.session.video
    rig.frame(10.1)
    replacement = VideoStore(source="device", endpoint="/dev/video98", clock=rig.session.clock)
    capture.attach_video(replacement, "new-source")
    rig.frame(10.2)  # Original source is detached, even if it still delivers.
    rig.now[0] = 10.3
    replacement.accept_raw(width=8, height=6, step=24, pixel_format="RGB_INT8", data=bytes(144), received_at=10.3)
    result = capture.stop(timeout=3.)
    assert result["camera"]["written_frames"] == 2
    frames = [json.loads(line) for line in (capture.directory / "camera.frames.jsonl").read_text().splitlines()]
    assert [row["source_id"] for row in frames] == [rig.session.video_source_id, "new-source"]
    assert not original._frame_sinks and not replacement._frame_sinks


def test_shutdown_finalizes_capture_and_next_take_gets_new_identifier(rig):
    first = start(rig).json()
    capture = rig.session.recorder.filming
    rig.frame(10.1)
    rig.session.recorder.stop(rig.now[0], reason="session_closed")
    capture.stop(timeout=3.)
    rig.session.recorder.visual.wait(3.)
    assert capture.status()["writer_stopped"]
    assert capture.status()["end_reason"] == "session_closed"
    assert start(rig).json()["id"] != first["id"]


def test_recording_sink_failure_does_not_stop_radio_status(rig):
    def broken(status):
        raise OSError("disk unavailable")
    received = []
    rig.radio.add_status_sink(broken)
    rig.radio.add_status_sink(received.append)
    for _ in range(3):
        rig.radio._publish(radio_status(rig.radio.clock()))
    assert len(received) == 3
    status = rig.radio.snapshot()
    assert status["connected"] and status["recording_sink_errors"] == 3


def test_writer_stop_during_attachment_cannot_leave_a_subscribed_capture(rig):
    assert start(rig).status_code == 200
    capture = rig.session.recorder.filming
    capture.stop(timeout=3.)
    capture.attach_radio(rig.radio)
    capture.attach_video(rig.session.video, rig.session.video_source_id)
    assert not rig.radio._status_sinks
    assert not rig.session.video._frame_sinks
