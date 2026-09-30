from pathlib import Path
import socket
import pytest

from argos import distance
from argos.backends.edgetx_distance_profile import prepare_profile
from argos.backends.edgetx_profile import prepare_profile as yaw_profile
from test_edgetx_profile import manual


def test_preparation_binds_only_separate_profile_script_and_protocol(manual, tmp_path, monkeypatch):
    profile = tmp_path / "distance.yml"
    profile.write_bytes(prepare_profile(manual))
    monkeypatch.setattr(distance, "read_verified_model", lambda *a, **kw: None)
    monkeypatch.setattr(distance, "software_identity", lambda root: {"runtime_sha256": "test"})
    values = dict(profile=str(profile), camera_device="/dev/video999",
                  radio_port="/dev/ttyACM999", vision_model="model.onnx", port=8080)
    config, manifest = distance.prepare(values)
    assert config.yaw_assist and config.vision_hz == 10
    assert manifest["radio_protocol"] == "ARGOS_DISTANCE_STREAM_V3"
    assert manifest["radio_profile"]["model_name"] == "ARGOS DST"
    assert manifest["radio_script_sha256"] == distance._digest(Path("scripts/edgetx/ArgDst.lua"))
    profile.write_bytes(yaw_profile(manual))
    with pytest.raises(ValueError, match="ARGOS DST"):
        distance.prepare(values)


def test_experiment_refuses_occupied_console_before_opening_devices(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); sock.listen(1)
        with pytest.raises(OSError):
            with distance.instance(tmp_path, sock.getsockname()[1]):
                pytest.fail("Must not take over a running FLY server")
