"""Launch evidence remains useful without changing simulator lifecycle behavior."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples import run_web_control as launcher


@pytest.mark.parametrize("comm", ["ruby", "gz sim server", "worker ) (render)"])
def test_process_identity_handles_parenthesized_command_names(tmp_path, comm):
    stat = tmp_path / "123/stat"
    stat.parent.mkdir()
    fields = ["S", *(["0"] * 18), "456789", "0"]
    stat.write_text(f"123 ({comm}) {' '.join(fields)}\n")

    assert launcher.process_start_ticks(123, tmp_path) == 456789


@pytest.mark.parametrize(
    "content", [None, "", "123 (ruby) S", "123 (ruby) S " + "0 " * 18 + "invalid"]
)
def test_missing_or_invalid_process_identity_is_explicitly_unavailable(tmp_path, content):
    if content is not None:
        stat = tmp_path / "123/stat"
        stat.parent.mkdir()
        stat.write_text(content)

    assert launcher.process_start_ticks(123, tmp_path) is None


def test_provenance_preserves_invocation_and_tolerates_absent_proc(tmp_path, monkeypatch):
    argv = ["python3", "-u", "examples/run_web_control.py", "--gui"]
    monkeypatch.setattr(launcher.sys, "orig_argv", argv)
    monkeypatch.chdir(tmp_path)

    provenance = launcher.launch_provenance(True, tmp_path / "absent-proc")
    argv.append("later mutation")

    assert provenance == {
        "launcher_argv": ["python3", "-u", "examples/run_web_control.py", "--gui"],
        "launcher_cwd": str(tmp_path),
        "gazebo_mode": "gui",
        "boot_id": None,
        "launcher_start_ticks": None,
        "children": {},
    }


def test_provenance_pairs_process_start_with_boot_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.os, "getpid", lambda: 123)
    boot_id = tmp_path / "sys/kernel/random/boot_id"
    boot_id.parent.mkdir(parents=True)
    boot_id.write_text("01234567-89ab-cdef-0123-456789abcdef\n")
    stat = tmp_path / "123/stat"
    stat.parent.mkdir()
    stat.write_text("123 (ruby) S " + "0 " * 18 + "456789 0\n")

    provenance = launcher.launch_provenance(False, tmp_path)

    assert provenance["boot_id"] == "01234567-89ab-cdef-0123-456789abcdef"
    assert provenance["launcher_start_ticks"] == 456789
    assert provenance["gazebo_mode"] == "headless"


@pytest.mark.parametrize("gui", [False, True])
@pytest.mark.parametrize("variant", [None, "tiny", "s"])
@pytest.mark.parametrize("threads", [None, 4])
def test_manifest_preserves_legacy_pids_and_records_each_actual_launch(tmp_path, monkeypatch, gui, variant, threads):
    # A complete offline launcher pass checks the Popen-to-manifest connection.
    # No real ports, simulator processes or signals are used.
    ardupilot = tmp_path / "ardupilot"
    binary = ardupilot / "build/sitl/bin/arducopter"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    gazebo = tmp_path / "Gazebo models"
    model = gazebo / "models/iris_with_gimbal/model.sdf"
    model.parent.mkdir(parents=True)
    model.write_text(
        '<sdf><model><plugin name="ArduPilotPlugin">'
        '<fdm_port_in>9002</fdm_port_in></plugin></model></sdf>'
    )
    world = gazebo / "worlds/iris_runway.sdf"
    world.parent.mkdir()
    world.write_text('<sdf><world name="iris_runway" /></sdf>')
    run = tmp_path / "argos-web-control-evidence"
    run.mkdir()
    repo = Path(launcher.__file__).resolve().parents[1]
    argv = [
        "examples/run_web_control.py",
        "--ardupilot-dir", str(ardupilot),
        "--gazebo-dir", str(gazebo),
    ]
    if gui:
        argv.append("--gui")
    if threads is not None:
        argv += ["--vision-threads", str(threads)]
    if variant is not None:
        detector = tmp_path / "detector.onnx"
        detector.write_bytes(b"the console worker verifies model contents")
        argv += ["--vision-model", str(detector), "--vision-variant", variant]
    monkeypatch.setattr(launcher.sys, "argv", argv)
    monkeypatch.setattr(launcher.sys, "orig_argv", ["python3", "-u", *argv])
    monkeypatch.setattr(launcher.shutil, "which", lambda command: "/usr/bin/gz")
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda **kwargs: str(run))
    monkeypatch.setattr(launcher.signal, "signal", lambda *args: None)
    monkeypatch.setattr(launcher, "process_start_ticks", lambda pid, *args: pid * 10)

    processes = []
    launches = []
    signals = []

    def start(command, **kwargs):
        process = SimpleNamespace(pid=1001 + len(processes), returncode=None)
        process.poll = lambda: process.returncode

        def wait(**kwargs):
            process.returncode = 0
            return 0

        process.wait = wait
        processes.append(process)
        launches.append((list(command), kwargs))
        return process

    def probe_port(*args):
        if len(processes) >= 2:
            raise OSError("fake SITL is listening")

    def interrupt_after_startup(seconds):
        assert seconds == .5
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    monkeypatch.setattr(launcher, "free_port", probe_port)
    monkeypatch.setattr(launcher.time, "sleep", interrupt_after_startup)
    monkeypatch.setattr(launcher.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    launcher.main()

    manifest = json.loads((run / "processes.json").read_text())
    provenance = manifest.pop("provenance")
    assert manifest == {
        "launcher": launcher.os.getpid(),
        "children": {"gazebo": 1001, "sitl": 1002, "console": 1003},
        "partition": run.name,
        "http_port": 8081,
        "mavlink_port": 5860,
        "physics_port": 9004,
        "scene": "runway",
    }
    assert provenance["launcher_argv"] == ["python3", "-u", *argv]
    assert provenance["gazebo_mode"] == ("gui" if gui else "headless")
    expected_gazebo = ["gz", "sim", "-r"]
    if not gui:
        expected_gazebo += ["-s", "--headless-rendering"]
    assert launches[0][0] == [*expected_gazebo, str(run / "manual-flight.sdf")]
    console_argv = launches[2][0]
    if variant is None:
        assert "--vision-variant" not in console_argv
        assert "--vision-threads" not in console_argv
    else:
        assert console_argv[console_argv.index("--vision-variant") + 1] == variant
        assert console_argv[console_argv.index("--vision-model") + 1] == str(detector)
        assert console_argv[console_argv.index("--vision-threads") + 1] == str(threads or 2)
    expected_working_dirs = (("gazebo", run), ("sitl", run / "sitl"), ("console", repo))
    for index, (name, cwd) in enumerate(expected_working_dirs):
        assert provenance["children"][name] == {
            "argv": launches[index][0],
            "cwd": str(cwd),
            "start_ticks": (1001 + index) * 10,
        }
        assert launches[index][1]["cwd"] == cwd
        assert launches[index][1]["start_new_session"] is True
    assert signals == [(pid, launcher.signal.SIGINT) for pid in (1003, 1002, 1001)]
