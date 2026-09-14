"""Isolation and failure paths for the opt-in simulator acceptance bench."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import socket
import subprocess
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from examples import run_radio_bench as launcher


def parameters(path):
    result = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            key, value = line.split()
            assert key not in result, f"duplicate parameter: {key}"
            result[key] = float(value)
    return result


def test_radio_profile_preserves_gps_free_flight_and_changes_authority_explicitly():
    base = parameters(launcher.REPO / "examples/sitl-web-control.parm")
    bench = parameters(launcher.REPO / "examples/sitl-radio-bench.parm")
    changes = {name: value for name, value in bench.items() if name in base and base[name] != value}
    assert changes == {"FLTMODE_CH": 5, "RC_OVERRIDE_TIME": .5, "FS_GCS_ENABLE": 0}
    assert bench["FS_THR_ENABLE"] == 3
    assert bench["RC_OPTIONS"] == bench["PILOT_THR_BHV"] == bench["FS_OPTIONS"] == 0
    assert [bench[f"FLTMODE{number}"] for number in (1, 4, 6)] == [0, 2, 9]
    assert bench["RC6_OPTION"] == 153
    assert bench["RC7_OPTION"] == 46
    assert not any(name.startswith("ARMING_") for name in bench)
    assert bench["SIM_ADSB_COUNT"] == -1
    for number in (0, 1, 2):
        assert bench[f"SERIAL{number}_PROTOCOL"] == 2
        for stream in ("EXT_STAT", "EXTRA1", "EXTRA3", "RC_CHAN"):
            assert bench[f"MAV{number + 1}_{stream}"] > 0


@pytest.fixture
def installation(tmp_path, monkeypatch):
    ardupilot = tmp_path / "ardupilot"
    binary = ardupilot / "build/sitl/bin/arducopter"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"existing binary, not compiled by this test")
    binary.chmod(0o755)
    (ardupilot / "build/sitl/ap_version.h").write_text('#define GIT_VERSION "old-build"\n')
    gazebo = tmp_path / "gazebo"
    vehicle = gazebo / "models/iris_with_gimbal/model.sdf"
    vehicle.parent.mkdir(parents=True)
    vehicle.write_text('<sdf><model><plugin name="ArduPilotPlugin">'
                       '<fdm_addr>192.0.2.3</fdm_addr><fdm_port_in>9002</fdm_port_in>'
                       '</plugin></model></sdf>')
    camera = gazebo / "models/gimbal_small_3d/model.sdf"
    camera.parent.mkdir()
    camera.write_text('<sdf><model><sensor name="camera">'
                      '<camera><horizontal_fov>1.2</horizontal_fov></camera>'
                      '<plugin filename="CameraZoomPlugin" /></sensor></model></sdf>')
    world = gazebo / "worlds/iris_runway.sdf"
    world.parent.mkdir()
    world.write_text('<sdf><world name="iris_runway"><model name="axes" />'
                     '<include><uri>model://runway</uri></include></world></sdf>')
    mesh = tmp_path / "walk.dae"
    mesh.write_bytes(b"verified elsewhere")
    model = tmp_path / "detector.onnx"
    model.write_bytes(b"detector is verified in the actual console worker")
    monkeypatch.setattr(launcher, "verified_mesh", lambda path: mesh)
    monkeypatch.setattr(launcher.shutil, "which", lambda program: "/usr/bin/gz")
    monkeypatch.setattr(launcher, "free_port", lambda *args: None)
    argv = ["--ardupilot-dir", str(ardupilot), "--gazebo-dir", str(gazebo),
            "--vision-model", str(model)]
    return SimpleNamespace(argv=argv, ardupilot=ardupilot, binary=binary,
                           gazebo=gazebo, mesh=mesh, model=model)


@pytest.mark.parametrize("arguments", [[], ["--vision-threads", "0"], ["--vision-variant", "unverified"]])
def test_invalid_detector_arguments_are_rejected(arguments):
    with pytest.raises(SystemExit):
        launcher.parser().parse_args(arguments)


@pytest.mark.parametrize("overrides", [
    ["--rc-port", "5960"], ["--physics-port", "8083"], ["--monitor-port", "0"],
    ["--assistance-port", "65536"],
])
def test_ports_cannot_alias_independent_inputs(installation, overrides):
    args = launcher.parser().parse_args(installation.argv + overrides)
    with pytest.raises(ValueError, match="ports"):
        launcher.validate(args)


def test_busy_port_rejected_before_any_child_is_started(installation, monkeypatch):
    def unavailable(port, kind):
        if port == 5521:
            assert kind == socket.SOCK_DGRAM
            raise OSError("address in use")
    monkeypatch.setattr(launcher, "free_port", unavailable)
    with pytest.raises(ValueError, match="5521.*unavailable"):
        launcher.validate(launcher.parser().parse_args(installation.argv))


def test_output_directory_never_reuses_captures(tmp_path):
    output = tmp_path / "retained run"
    output.mkdir()
    recording = output / "recording.jsonl"
    recording.write_bytes(b"keep exactly")
    with pytest.raises(ValueError, match="new or empty"):
        launcher.create_output_dir(output)
    assert recording.read_bytes() == b"keep exactly"
    assert launcher.create_output_dir(tmp_path / "new") == tmp_path / "new"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert launcher.create_output_dir(empty) == empty


@pytest.mark.parametrize("scene", ["person", "inspection"])
def test_scene_changes_only_private_copies(installation, tmp_path, scene):
    args = launcher.parser().parse_args(installation.argv + ["--scene", scene])
    before = {path: path.read_bytes() for path in installation.gazebo.rglob("*") if path.is_file()}
    output = tmp_path / "run"
    output.mkdir()
    world = launcher.prepare_scene(args, output, installation.gazebo, installation.mesh)
    vehicle = ET.parse(output / "models/iris_with_gimbal/model.sdf")
    assert vehicle.findtext(".//fdm_addr") == "127.0.0.1"
    assert vehicle.findtext(".//fdm_port_in") == "9024"
    camera = ET.parse(output / "models/gimbal_small_3d/model.sdf")
    assert camera.find(".//plugin[@filename='CameraZoomPlugin']") is None
    assert camera.findtext(".//horizontal_fov") == "1.2"
    assert ET.parse(world).find(".//model[@name='axes']") is None
    assert ET.parse(world).find(".//actor") is not None
    assert before == {path: path.read_bytes() for path in before}


def fake_processes(monkeypatch, *, fail_console=False):
    launched, signaled = [], []

    def start(command, **kwargs):
        if fail_console and "argos.console" in command:
            raise OSError("console launch failed")
        process = SimpleNamespace(pid=100000 + len(launched), returncode=None)
        process.poll = lambda: process.returncode
        def wait(**kwargs):
            process.returncode = 0
            return 0
        process.wait = wait
        launched.append((list(command), kwargs, process))
        return process

    def probe(command, **kwargs):
        if command[-1] == "--help":
            return subprocess.CompletedProcess(command, 0, stdout="--rc-in-port PORT", stderr="")
        value = "source-head-different-from-build" if command[-1] == "HEAD" else ""
        return subprocess.CompletedProcess(command, 0, stdout=value, stderr="")

    monkeypatch.setattr(launcher.subprocess, "run", probe)
    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    monkeypatch.setattr(launcher, "process_start_ticks", lambda pid: pid * 10)
    monkeypatch.setattr(launcher.os, "killpg", lambda pid, sig: signaled.append((pid, sig)))
    monkeypatch.setattr(launcher, "wait_for_sitl", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "wait_for_console", lambda *args: None)
    return launched, signaled


@pytest.mark.parametrize("outcome", ["passed", "failed", "crashed", "interrupted", "console_failed"])
def test_every_outcome_stops_own_children_and_retains_evidence(installation, tmp_path, monkeypatch, outcome):
    output = tmp_path / "isolated run"
    launched, signaled = fake_processes(monkeypatch, fail_console=outcome == "console_failed")
    waited = []
    monkeypatch.setattr(launcher, "wait_for_sitl", lambda children, port, **kwargs: waited.append(port))
    scenario_arguments = []
    def scenario(**kwargs):
        scenario_arguments.append(kwargs)
        if outcome == "crashed":
            raise RuntimeError("scenario error")
        if outcome == "interrupted":
            raise KeyboardInterrupt
        return {"passed": outcome == "passed", "observations": ["retained"]}
    monkeypatch.setattr(launcher, "run_scenario", scenario)
    code = launcher.main(installation.argv + ["--output-dir", str(output), "--vision-variant", "s",
                                              "--vision-threads", "4"])
    assert code == (0 if outcome == "passed" else 1)
    report = json.loads((output / "report.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert report["passed"] is (outcome == "passed")
    assert manifest["infrastructure_stopped"] is True
    assert manifest["console_control"] is False
    assert manifest["firmware"]["source_head"] == "source-head-different-from-build"
    assert "old-build" in manifest["firmware"]["version_header"]
    assert manifest["firmware"]["rebuilt_by_launcher"] is False
    assert manifest["firmware"]["source_is_binary_build_proof"] is False
    assert manifest["firmware"]["binary_sha256"] == launcher.sha256(installation.binary)
    assert manifest["vision"]["variant"] == "s"
    assert manifest["vision"]["threads"] == 4
    assert (output / "recordings").is_dir()
    for command, kwargs, process in launched:
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["GZ_PARTITION"] == manifest["partition"]
        assert kwargs["env"]["PYTHONPATH"].split(":")[0] == str(launcher.REPO)
        assert (process.pid, signal.SIGINT) in signaled
        assert kwargs["stdout"].closed
    assert {pid for pid, sig in signaled} == {process.pid for _, _, process in launched}
    sitl = launched[1][0]
    assert sitl[sitl.index("--rc-in-port") + 1] == "5521"
    assert [sitl[sitl.index(f"--serial{n}") + 1] for n in range(3)] == [
        "tcp:5960:wait", "tcp:5962", "tcp:5964",
    ]
    if outcome != "console_failed":
        assert waited == [5960, 5962, 5964]
        console = launched[2][0]
        assert "--sim-control" not in console and "--sim-framing" not in console
        assert console[console.index("--recordings-dir") + 1] == str(output / "recordings")
        assert scenario_arguments[0]["mavlink_peer"] == ("127.0.0.1", 5964)
        assert scenario_arguments[0]["assistance_peer"] == ("127.0.0.1", 5962)
        assert scenario_arguments[0]["rc_peer"] == ("127.0.0.1", 5521)
    else:
        assert not scenario_arguments


def test_no_simulators_start_if_binary_lacks_native_rc_flag(installation, tmp_path, monkeypatch):
    launched, signaled = fake_processes(monkeypatch)
    monkeypatch.setattr(launcher.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, stdout="old help", stderr=""))
    output = tmp_path / "run"
    assert launcher.main(installation.argv + ["--output-dir", str(output)]) == 1
    assert not launched and not signaled
    report = json.loads((output / "report.json").read_text())
    assert "--rc-in-port" in report["error"]
    assert (output / "sitl-help.txt").read_text() == "old help"


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_escaping_scenario_failure_preserves_saved_checks_and_capture(installation, tmp_path, monkeypatch, failure):
    output = tmp_path / "partial run"
    launched, signaled = fake_processes(monkeypatch)
    partial = {"schema_version": 1, "passed": False,
               "checks": [{"name": "firmware_switch_rejection", "passed": True}],
               "recording": {"id": "recorded-session"},
               "recording_final": {"active": False}, "error": "original scenario failure"}

    def scenario(**kwargs):
        launcher.write_json(output / "report.json", partial)
        raise failure("failure after scenario saved its report")

    monkeypatch.setattr(launcher, "run_scenario", scenario)
    assert launcher.main(installation.argv + ["--output-dir", str(output)]) == 1
    retained = json.loads((output / "report.json").read_text())
    for key, value in partial.items():
        assert retained[key] == value
    assert retained["launcher_error"]
    if failure is KeyboardInterrupt:
        assert retained["interrupted"] is True
    assert all((process.pid, signal.SIGINT) in signaled for _, _, process in launched)


def test_cleanup_failure_keeps_report_fields_accessible(installation, tmp_path, monkeypatch):
    output = tmp_path / "cleanup run"
    fake_processes(monkeypatch)
    report = {"passed": True, "checks": [{"name": "landed_disarmed", "passed": True}],
              "recording_final": {"active": False}}
    monkeypatch.setattr(launcher, "run_scenario", lambda **kwargs: report)
    stop = launcher.Children.stop

    def stop_then_fail(children):
        stop(children)
        raise OSError("cleanup report failed")

    monkeypatch.setattr(launcher.Children, "stop", stop_then_fail)
    assert launcher.main(installation.argv + ["--output-dir", str(output)]) == 1
    retained = json.loads((output / "report.json").read_text())
    assert retained["passed"] is False
    assert retained["checks"] == report["checks"]
    assert retained["recording_final"] == report["recording_final"]
    assert retained["cleanup_error"] == "cleanup report failed"
