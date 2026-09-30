from argparse import Namespace
import json
from pathlib import Path
import socket
import threading

import pytest

from argos import fly
from argos.console.config import ConsoleConfig
from argos.console.yaw_assist import YawAssistService


def arguments(**values):
    return Namespace(config=None, camera_device=None, radio_port=None, profile=None,
                     vision_model=None, port=None, **values)


def test_settings_reject_unknown_fields_and_accept_explicit_overrides(tmp_path):
    path = tmp_path / "fly.json"
    path.write_text(json.dumps({"camera_device": "/dev/video2", "radio_port": "/dev/ttyACM0",
                                "profile": str(tmp_path / "model.yml"), "port": 8080}))
    args = arguments()
    args.config, args.port = path, 8081
    assert fly.load_settings(args)["port"] == 8081
    path.write_text('{"shell_command": "no"}')
    with pytest.raises(ValueError, match="unknown"):
        fly.load_settings(args)


def test_socket_conflict_is_detected_before_workers_start(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with pytest.raises(OSError):
            with fly.instance(tmp_path, occupied.getsockname()[1]):
                pytest.fail("An occupied HTTP port must not start a serial worker")


def test_instance_lock_prevents_second_owner_and_releases_after_close(tmp_path):
    with fly.instance(tmp_path, 0):
        with pytest.raises(RuntimeError, match="already running"):
            with fly.instance(tmp_path, 0):
                pytest.fail("second owner")
    with fly.instance(tmp_path, 0):
        pass


def test_fly_configuration_requires_physical_vision():
    with pytest.raises(ValueError, match="physical camera"):
        ConsoleConfig(yaw_assist=True)


def test_service_runs_serial_outside_http_and_stale_status_cannot_show_active(tmp_path):
    started = threading.Event()
    now = [10.]
    caller = threading.get_ident()
    def runner(device, source, *, stop_event, on_status, report):
        assert threading.get_ident() != caller
        on_status({"connected": True, "radio_state": "A", "radio_cause": "A",
                   "reason": "active", "radio_a_to_t_total": 2})
        started.set()
        stop_event.wait(2.)
    service = YawAssistService("/dev/test", 8080, runner=runner,
                              source_factory=lambda **kw: None,
                              directory=tmp_path, clock=lambda: now[0])
    try:
        service.start()
        assert started.wait(1.)
        snapshot = service.snapshot()
        assert snapshot["radio_state"] == "A"
        assert snapshot["radio_a_to_t_total"] == 2
        now[0] += 2.
        assert service.snapshot()["radio_state"] is None
        assert not service.snapshot()["connected"]
    finally:
        service.close()
    assert service.snapshot()["reason"] == "Stream stopped"
    assert json.loads((tmp_path / "status.json").read_text())["radio_state"] == "A"


def test_service_logs_state_changes_and_throttles_steady_state(tmp_path):
    now = [10.]
    service = YawAssistService("/dev/test", 8080, directory=tmp_path, clock=lambda: now[0])
    status = {"connected": True, "radio_state": "A", "reason": "active"}
    service._publish(status)
    # Wait for the first asynchronous disk sample before producing a transition.
    import time
    limit = time.monotonic() + 1.
    while not (tmp_path / "events.jsonl").exists() and time.monotonic() < limit:
        time.sleep(.005)
    now[0] += .1
    service._publish(status)
    now[0] += .1
    service._publish(dict(status, radio_state="T", reason="target"))
    service.close()
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["radio_state"] == "T"


def test_stalled_disk_logger_never_blocks_radio_status_publication(tmp_path):
    entered, release = threading.Event(), threading.Event()
    service = YawAssistService("/dev/test", 8080, directory=tmp_path)
    def blocked(record):
        entered.set()
        release.wait(2.)
    service._write_log = blocked
    try:
        service._publish({"connected": True, "radio_state": "A", "reason": "active"})
        assert entered.wait(1.)
        for count in range(10):
            service._publish({"connected": True, "radio_state": "T", "reason": str(count)})
        assert service.snapshot()["reason"] == "9"
        assert service._log_dropped > 0
    finally:
        release.set()
        service.close()


def test_cli_check_opens_no_network_or_serial(monkeypatch, capsys):
    monkeypatch.setattr(fly, "prepare", lambda values: (None, {"verified": True}))
    monkeypatch.setattr(fly.socket, "socket", lambda *a, **kw: pytest.fail("socket opened"))
    assert fly.main(["--camera-device", "/dev/video2", "--radio-port", "/dev/ttyACM0",
                     "--profile", "/tmp/example.yml", "--check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"verified": True}


def test_manifest_fingerprints_actual_runtime_changes(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "argos/console/static").mkdir(parents=True)
    (tmp_path / "scripts/edgetx").mkdir(parents=True)
    files = ("argos/main.py", "argos/console/static/app.js", "scripts/edgetx/ArgFly.lua", "pyproject.toml")
    for name in files:
        (tmp_path / name).write_text("first\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    before = fly.software_identity(tmp_path)
    (tmp_path / "argos/main.py").write_text("second\n")
    after = fly.software_identity(tmp_path)
    assert before["git_revision"] == after["git_revision"]
    assert not before["git_dirty"] and after["git_dirty"]
    assert before["runtime_sha256"] != after["runtime_sha256"]
