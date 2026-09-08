#!/usr/bin/env python3
"""Run an isolated GPS-free Gazebo/SITL session with ARGOS web controls.

Uses the pinned installations in docs/sitl-observation.md. Creates a private
model copy and fresh SITL parameter storage; never changes an installed model,
the observation session or a physical controller. Ctrl-C stops only these children.
"""
from __future__ import annotations

import argparse
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
import xml.etree.ElementTree as ET


def free_port(port, kind):
    with socket.socket(socket.AF_INET, kind) as probe:
        if kind == socket.SOCK_STREAM:
            # Match the servers' restart behavior after a prior TCP connection.
            # Active listeners still prevent this bind; TIME_WAIT does not.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def main():
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ardupilot-dir", type=Path, default=repo.parent / "ardupilot")
    parser.add_argument("--gazebo-dir", type=Path, default=repo.parent / "ardupilot_gazebo")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--mavlink-port", type=int, default=5860)
    parser.add_argument("--physics-port", type=int, default=9004)
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()
    for port in (args.port, args.mavlink_port, args.physics_port):
        if not 1024 <= port <= 65535:
            parser.error("ports must be between 1024 and 65535")
    if args.port == args.mavlink_port:
        parser.error("HTTP and MAVLink ports must differ")
    binary = args.ardupilot_dir.resolve() / "build/sitl/bin/arducopter"
    gazebo = args.gazebo_dir.resolve()
    if not binary.is_file() or not (gazebo / "worlds/iris_runway.sdf").is_file():
        parser.error("install the pinned ArduPilot and Gazebo models first (docs/sitl-observation.md)")
    if not shutil.which("gz"):
        parser.error("Gazebo Harmonic is required")
    for port, kind in ((args.port, socket.SOCK_STREAM), (args.mavlink_port, socket.SOCK_STREAM),
                       (args.physics_port, socket.SOCK_DGRAM)):
        try:
            free_port(port, kind)
        except OSError as exc:
            parser.error(f"port {port} is unavailable: {exc}")

    run = Path(tempfile.mkdtemp(prefix="argos-web-control-"))
    models = run / "models"
    models.mkdir()
    shutil.copytree(gazebo / "models/iris_with_gimbal", models / "iris_with_gimbal")
    model = models / "iris_with_gimbal/model.sdf"
    tree = ET.parse(model)
    port_element = tree.find(".//plugin[@name='ArduPilotPlugin']/fdm_port_in")
    if port_element is None:
        parser.error("pinned ArduPilotPlugin model port was not found")
    port_element.text = str(args.physics_port)
    tree.write(model, encoding="unicode", xml_declaration=True)
    world = ET.parse(gazebo / "worlds/iris_runway.sdf")
    # Omit the upstream debug-coordinate visual; no artificial tracking markers.
    world_element = world.getroot().find("world")
    for item in list(world_element):
        if item.tag == "model" and item.get("name") == "axes":
            world_element.remove(item)
    world_file = run / "manual-flight.sdf"
    world.write(world_file, encoding="unicode", xml_declaration=True)
    sitl_dir = run / "sitl"
    sitl_dir.mkdir()
    env = dict(os.environ)
    env["GZ_PARTITION"] = run.name
    env["GZ_SIM_SYSTEM_PLUGIN_PATH"] = str(gazebo / "build") + ":" + env.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "")
    env["GZ_SIM_RESOURCE_PATH"] = ":".join(map(str, [models, gazebo / "models", gazebo / "worlds"]))
    # Pin this checkout even when using a venv installed editable from another one.
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    children = []
    logs = []

    def start(name, command, cwd=run):
        log = (run / f"{name}.log").open("w")
        logs.append(log)
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        children.append((name, process))
        (run / "processes.json").write_text(json.dumps({
            "launcher": os.getpid(), "children": {n: p.pid for n, p in children},
            "partition": env["GZ_PARTITION"], "http_port": args.port,
            "mavlink_port": args.mavlink_port, "physics_port": args.physics_port,
        }, indent=2))

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    print(f"Run files: {run}", flush=True)
    try:
        gz_command = ["gz", "sim", "-r"]
        if not args.gui:
            gz_command += ["-s", "--headless-rendering"]
        start("gazebo", gz_command + [str(world_file)])
        start("sitl", [str(binary), "--model", "JSON", "--speedup", "1", "-I", "10",
                       "--home=-35.363262,149.165237,584,0", "--sim-port-out", str(args.physics_port),
                       "--serial0", f"tcp:{args.mavlink_port}:wait",
                       "--defaults", str(repo / "examples/sitl-web-control.parm"), "--sysid", "1"], sitl_dir)
        # TCP open is retried by starting the console only once SITL binds it.
        # Do not connect a probe: the SITL server accepts one client and :wait
        # uses that connection to release startup. Inspect kernel listener state.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if any(process.poll() is not None for _, process in children):
                raise RuntimeError("simulator startup failed; see the run logs")
            try:
                free_port(args.mavlink_port, socket.SOCK_STREAM)
            except OSError:
                break
            time.sleep(.1)
        else:
            raise RuntimeError("SITL did not open its TCP port")
        start("console", [sys.executable, "-m", "argos.console", "--sim-control",
                          "--gazebo-topic", "/world/iris_runway/model/iris_with_gimbal/model/gimbal/link/pitch_link/sensor/camera/image",
                          "--gazebo-python-path", "/usr/lib/python3/dist-packages",
                          "--mavlink-tcp", f"127.0.0.1:{args.mavlink_port}",
                          "--sequence-scope", "channel", "--heartbeat-age", "2.5",
                          "--port", str(args.port)], repo)
        print(f"ARGOS: http://127.0.0.1:{args.port} — Flight controls (initialization may take a few seconds)", flush=True)
        while True:
            for name, process in children:
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited ({process.returncode}); see {run / (name + '.log')}")
            time.sleep(.5)
    except KeyboardInterrupt:
        print("Stopping this simulation.", flush=True)
    finally:
        # Console first: its shutdown can request a landing before sockets close.
        for _, process in reversed(children):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        for log in logs:
            log.close()
        print(f"Logs retained: {run}", flush=True)


if __name__ == "__main__":
    main()
