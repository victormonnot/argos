#!/usr/bin/env python3
"""Run the isolated GPS-free virtual-radio/assistance acceptance scenario.

Starts its own Gazebo, fresh ArduCopter SITL and passive ARGOS console. The
scenario owns the separate pilot and assistance processes. No hardware, installed
model, existing service or normal capture directory is changed. Run files are
retained after success, failure or interruption; all owned children are stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET

if __package__:
    from .run_web_control import (
        add_inspection_scene, add_person_scene, copy_fixed_camera, free_port,
        launch_provenance, process_start_ticks,
    )
    from .setup_vision_scene import default_assets_dir, verified_mesh
else:
    from run_web_control import (
        add_inspection_scene, add_person_scene, copy_fixed_camera, free_port,
        launch_provenance, process_start_ticks,
    )
    from setup_vision_scene import default_assets_dir, verified_mesh


REPO = Path(__file__).resolve().parents[1]
CAMERA_TOPIC = "/world/iris_runway/model/iris_with_gimbal/model/gimbal/link/pitch_link/sensor/camera/image"
PORTS = {
    "port": socket.SOCK_STREAM,
    "mavlink_port": socket.SOCK_STREAM,
    "assistance_port": socket.SOCK_STREAM,
    "monitor_port": socket.SOCK_STREAM,
    "physics_port": socket.SOCK_DGRAM,
    "rc_port": socket.SOCK_DGRAM,
}


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def failed_report(path: Path, current, error: str, *, interrupted=False) -> dict:
    """Retain scenario evidence if an exception escapes after its final write."""
    candidates = [current]
    try:
        candidates.insert(0, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    retained = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            json.dumps(candidate, allow_nan=False)
        except (TypeError, ValueError):
            continue
        retained = dict(candidate)
        break
    retained["passed"] = False
    retained.setdefault("error", error)
    retained["launcher_error"] = error
    if interrupted:
        retained["interrupted"] = True
    return retained


def firmware_provenance(ardupilot: Path, binary: Path) -> dict:
    """Describe the existing binary separately from its adjacent source tree."""
    header = ardupilot / "build/sitl/ap_version.h"
    result = {
        "binary": str(binary), "binary_sha256": sha256(binary),
        "rebuilt_by_launcher": False,
        "version_header_path": str(header),
        "version_header": header.read_text(encoding="utf-8") if header.is_file() else None,
        "source_head": None, "source_status": None,
        "source_is_binary_build_proof": False,
    }
    for key, arguments in (("source_head", ["rev-parse", "HEAD"]),
                           ("source_status", ["status", "--porcelain"])):
        try:
            completed = subprocess.run(["git", "-C", str(ardupilot), *arguments],
                                       capture_output=True, text=True, timeout=5, check=True)
            result[key] = completed.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--ardupilot-dir", type=Path, default=REPO.parent / "ardupilot")
    result.add_argument("--gazebo-dir", type=Path, default=REPO.parent / "ardupilot_gazebo")
    result.add_argument("--output-dir", type=Path, help="new or empty run directory; otherwise a private temporary directory")
    result.add_argument("--port", type=int, default=8083, help="passive loopback console HTTP port")
    result.add_argument("--mavlink-port", type=int, default=5960, help="SITL serial0 for the passive console")
    result.add_argument("--assistance-port", type=int, default=5962, help="SITL serial1 for the assistance process")
    result.add_argument("--monitor-port", type=int, default=5964, help="SITL serial2 for scenario observations")
    result.add_argument("--physics-port", type=int, default=9024)
    result.add_argument("--rc-port", type=int, default=5521, help="native SITL RC UDP input")
    result.add_argument("--gui", action="store_true")
    result.add_argument("--scene", choices=("person", "inspection"), default="person")
    result.add_argument("--person-assets", type=Path, default=default_assets_dir())
    result.add_argument("--vision-model", type=Path, required=True)
    result.add_argument("--vision-variant", choices=("tiny", "s"), default="tiny")
    result.add_argument("--vision-threads", type=int, choices=range(1, 7), default=2)
    return result


def validate(args) -> tuple[Path, Path, Path]:
    values = [getattr(args, name) for name in PORTS]
    if any(not 1024 <= port <= 65535 for port in values):
        raise ValueError("ports must be between 1024 and 65535")
    if len(set(values)) != len(values):
        raise ValueError("all six bench ports must differ")
    ardupilot = args.ardupilot_dir.expanduser().resolve()
    binary = ardupilot / "build/sitl/bin/arducopter"
    gazebo = args.gazebo_dir.expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("an existing executable ArduCopter SITL binary is required")
    if not (gazebo / "worlds/iris_runway.sdf").is_file() or not shutil.which("gz"):
        raise ValueError("install the pinned Gazebo models and Gazebo Harmonic first (docs/sitl-observation.md)")
    args.vision_model = args.vision_model.expanduser().resolve()
    if not args.vision_model.is_file():
        raise ValueError("--vision-model must name an existing local detector model")
    mesh = verified_mesh(args.person_assets.expanduser().resolve())
    for name, kind in PORTS.items():
        port = getattr(args, name)
        try:
            free_port(port, kind)
        except OSError as exc:
            raise ValueError(f"port {port} is unavailable: {exc}") from exc
    return binary, gazebo, mesh


def create_output_dir(requested: Path | None) -> Path:
    if requested is None:
        return Path(tempfile.mkdtemp(prefix="argos-radio-bench-"))
    run = requested.expanduser().resolve()
    if run.exists():
        if not run.is_dir() or any(run.iterdir()):
            raise ValueError("--output-dir must be a new or empty directory")
    else:
        run.mkdir(parents=True)
    return run


def prepare_scene(args, run: Path, gazebo: Path, mesh: Path) -> Path:
    models = run / "models"
    models.mkdir()
    shutil.copytree(gazebo / "models/iris_with_gimbal", models / "iris_with_gimbal")
    model = models / "iris_with_gimbal/model.sdf"
    tree = ET.parse(model)
    plugin = tree.find(".//plugin[@name='ArduPilotPlugin']")
    if plugin is None or plugin.find("fdm_port_in") is None:
        raise ValueError("pinned ArduPilotPlugin model port was not found")
    plugin.find("fdm_port_in").text = str(args.physics_port)
    address = plugin.find("fdm_addr")
    if address is None:
        address = ET.SubElement(plugin, "fdm_addr")
    address.text = "127.0.0.1"
    tree.write(model, encoding="unicode", xml_declaration=True)
    world = ET.parse(gazebo / "worlds/iris_runway.sdf")
    world_element = world.getroot().find("world")
    if world_element is None or world_element.get("name") != "iris_runway":
        raise ValueError("pinned iris_runway world was not found")
    for item in list(world_element):
        if item.tag == "model" and item.get("name") == "axes":
            world_element.remove(item)
    if args.scene == "inspection":
        add_inspection_scene(world_element, models)
    copy_fixed_camera(gazebo, models)
    add_person_scene(world_element, models, mesh)
    world_file = run / "radio-bench.sdf"
    world.write(world_file, encoding="unicode", xml_declaration=True)
    return world_file


def commands(args, run: Path, binary: Path, world: Path) -> dict[str, list[str]]:
    gazebo = ["gz", "sim", "-r"]
    if not args.gui:
        gazebo += ["-s", "--headless-rendering"]
    return {
        "gazebo": [*gazebo, str(world)],
        "sitl": [str(binary), "--model", "JSON", "--speedup", "1", "-I", "12",
                 "--home=-35.363262,149.165237,584,0", "--sim-address", "127.0.0.1",
                 "--sim-port-out", str(args.physics_port), "--rc-in-port", str(args.rc_port),
                 "--serial0", f"tcp:{args.mavlink_port}:wait",
                 "--serial1", f"tcp:{args.assistance_port}",
                 "--serial2", f"tcp:{args.monitor_port}",
                 "--defaults", str(run / "sitl-radio-bench.parm"), "--sysid", "1"],
        "console": [sys.executable, "-m", "argos.console",
                    "--gazebo-topic", CAMERA_TOPIC,
                    "--gazebo-python-path", "/usr/lib/python3/dist-packages",
                    "--mavlink-tcp", f"127.0.0.1:{args.mavlink_port}",
                    "--sequence-scope", "channel", "--heartbeat-age", "2.5",
                    "--port", str(args.port), "--recordings-dir", str(run / "recordings"),
                    "--vision-model", str(args.vision_model),
                    "--vision-variant", args.vision_variant,
                    "--vision-threads", str(args.vision_threads)],
    }


class Children:
    """Own only new session leaders created by this launcher."""

    def __init__(self, run: Path, env: dict, manifest: dict):
        self.run, self.env, self.manifest = run, env, manifest
        self.processes = []
        self.logs = []

    def save(self):
        write_json(self.run / "manifest.json", self.manifest)

    def start(self, name: str, command: list[str], cwd: Path):
        log = (self.run / f"{name}.log").open("w", encoding="utf-8")
        self.logs.append(log)
        process = subprocess.Popen(command, cwd=cwd, env=self.env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append((name, process))
        self.manifest["provenance"]["children"][name] = {
            "pid": process.pid, "argv": list(command), "cwd": str(cwd.resolve()),
            "start_ticks": process_start_ticks(process.pid),
            "log": str(self.run / f"{name}.log"),
        }
        self.save()

    def check(self):
        for name, process in self.processes:
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited ({process.returncode}); see {self.run / (name + '.log')}")

    def stop(self):
        failures = []
        for name, process in reversed(self.processes):
            # Signal the owned group even if its leader exited: Gazebo can have
            # remaining children. Never enumerate or signal unrelated services.
            try:
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                self.manifest["provenance"]["children"][name]["returncode"] = process.returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{name}: {exc}")
        for log in self.logs:
            log.close()
        self.manifest["infrastructure_stopped"] = not failures
        if failures:
            self.manifest["cleanup_errors"] = failures
        self.save()
        if failures:
            raise RuntimeError("infrastructure cleanup incomplete: " + "; ".join(failures))


def wait_for_sitl(children: Children, port: int, timeout: float = 20):
    # A connection probe would consume serial0's single-client :wait startup.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        children.check()
        try:
            free_port(port, socket.SOCK_STREAM)
        except OSError:
            return
        time.sleep(.1)
    raise RuntimeError(f"SITL did not open TCP port {port}")


def wait_for_console(children: Children, url: str, timeout: float = 45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        children.check()
        try:
            with urllib.request.urlopen(url + "/api/state", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(.2)
    raise RuntimeError("passive console did not become ready")


def run_scenario(**kwargs):
    if __package__:
        from .radio_bench_scenario import run_scenario as run
    else:
        from radio_bench_scenario import run_scenario as run
    return run(**kwargs)


def main(argv=None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        binary, gazebo, mesh = validate(args)
        run = create_output_dir(args.output_dir)
    except (ValueError, OSError) as exc:
        cli.error(str(exc))
    print(f"Run files: {run}", flush=True)
    env = dict(os.environ)
    env["GZ_PARTITION"] = "argos-radio-bench-" + uuid.uuid4().hex
    env["GZ_SIM_SYSTEM_PLUGIN_PATH"] = str(gazebo / "build") + os.pathsep + env.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "")
    env["GZ_SIM_RESOURCE_PATH"] = os.pathsep.join(map(str, [run / "models", gazebo / "models", gazebo / "worlds"]))
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    manifest = {
        "schema_version": 1, "purpose": "SITL virtual radio acceptance bench",
        "launcher_pid": os.getpid(),
        "output_dir": str(run), "partition": env["GZ_PARTITION"],
        "ports": {name: getattr(args, name) for name in PORTS},
        "scene": args.scene, "console_control": False, "physical_validation": False,
        "network": {"client_host": "127.0.0.1", "http_bind": "127.0.0.1",
                    "sitl_listener_scope": "native SITL TCP/RC listeners may bind wildcard; no host binding option"},
        "provenance": launch_provenance(args.gui),
        "infrastructure_stopped": False,
    }
    children = Children(run, env, manifest)
    previous_signals = {}
    report = {"passed": False}

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    for number in (signal.SIGINT, signal.SIGTERM):
        previous_signals[number] = signal.signal(number, interrupted)
    try:
        children.save()
        # Check the actual binary, without inferring its options from source HEAD.
        help_result = subprocess.run([str(binary), "--help"], cwd=run, capture_output=True,
                                     text=True, timeout=5, check=True)
        (run / "sitl-help.txt").write_text(help_result.stdout + help_result.stderr, encoding="utf-8")
        if "--rc-in-port" not in help_result.stdout + help_result.stderr:
            raise RuntimeError("the existing SITL binary does not expose --rc-in-port")
        manifest["firmware"] = firmware_provenance(binary.parents[3], binary)
        manifest["vision"] = {"model": str(args.vision_model), "sha256": sha256(args.vision_model),
                              "variant": args.vision_variant, "threads": args.vision_threads}
        manifest["person_mesh"] = {"path": str(mesh), "sha256": sha256(mesh)}
        world = prepare_scene(args, run, gazebo, mesh)
        (run / "sitl").mkdir()
        (run / "recordings").mkdir()
        profile = run / "sitl-radio-bench.parm"
        shutil.copyfile(REPO / "examples/sitl-radio-bench.parm", profile)
        manifest["profile"] = {"path": str(profile), "sha256": sha256(profile)}
        manifest["world"] = {"path": str(world), "sha256": sha256(world), "camera_topic": CAMERA_TOPIC,
                             "fixed_camera": True, "installed_gazebo_dir": str(gazebo)}
        children.save()
        launch = commands(args, run, binary, world)
        children.start("gazebo", launch["gazebo"], run)
        children.start("sitl", launch["sitl"], run / "sitl")
        wait_for_sitl(children, args.mavlink_port)
        children.start("console", launch["console"], REPO)
        console_url = f"http://127.0.0.1:{args.port}"
        print(f"Passive ARGOS console: {console_url}", flush=True)
        wait_for_console(children, console_url)
        # HTTP availability precedes SITL's sensor initialization. The other
        # UARTs bind only after serial0 releases startup and Gazebo sends data.
        wait_for_sitl(children, args.assistance_port, timeout=25)
        wait_for_sitl(children, args.monitor_port, timeout=25)
        report = run_scenario(console_url=console_url,
                              mavlink_peer=("127.0.0.1", args.monitor_port),
                              assistance_peer=("127.0.0.1", args.assistance_port),
                              rc_peer=("127.0.0.1", args.rc_port),
                              output_dir=run, python=sys.executable, env=env)
        if not isinstance(report, dict) or not isinstance(report.get("passed"), bool):
            raise RuntimeError("scenario did not return a report with a boolean passed field")
        json.dumps(report, allow_nan=False)
    except KeyboardInterrupt:
        report = failed_report(run / "report.json", report, "Launcher interrupted", interrupted=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        report = failed_report(run / "report.json", report, error)
        print(error, file=sys.stderr, flush=True)
    finally:
        for number in previous_signals:
            signal.signal(number, signal.SIG_IGN)
        try:
            try:
                write_json(run / "report.json", report)
            finally:
                try:
                    children.stop()
                except Exception as exc:
                    report = {**report, "passed": False, "cleanup_error": str(exc)}
                    write_json(run / "report.json", report)
        finally:
            for number, handler in previous_signals.items():
                signal.signal(number, handler)
            print(f"Report and logs retained: {run}", flush=True)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
