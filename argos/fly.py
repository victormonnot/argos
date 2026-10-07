"""Launch the local ARGOS FLY console and continuous Pocket yaw stream together."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys

from argos.console.config import ConsoleConfig
from argos.perception.yolox import default_model_path, get_model_spec, read_verified_model
from argos.perception.model_bundle import read_model_bundle


ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"camera_device", "radio_port", "profile", "vision_model", "vision_bundle", "port"}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def software_identity(root=ROOT):
    """Identify the checkout and actual runtime bytes, including uncommitted work."""
    root = Path(root)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=5.).strip()
    try:
        revision = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Launch ARGOS FLY from its Git checkout") from None
    files = sorted(path for path in (root / "argos").rglob("*")
                   if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    files += [root / "scripts/edgetx/ArgFly.lua", root / "pyproject.toml"]
    hashes = {str(path.relative_to(root)): _digest(path) for path in files}
    combined = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {"git_revision": revision, "git_dirty": dirty, "runtime_sha256": combined,
            "files": hashes}


def load_settings(args):
    values = {}
    if args.config is not None:
        values = json.loads(args.config.expanduser().read_text())
        if not isinstance(values, dict) or set(values) - FIELDS:
            raise ValueError("Fly configuration has unknown fields")
    for field in FIELDS:
        value = getattr(args, field, None)
        if value is not None:
            values[field] = str(value) if isinstance(value, Path) else value
    # Explicit CLI selection also replaces a saved selection of the other kind.
    if getattr(args, "vision_bundle", None) is not None:
        values.pop("vision_model", None)
    elif getattr(args, "vision_model", None) is not None:
        values.pop("vision_bundle", None)
    for field in ("camera_device", "radio_port", "profile"):
        if not isinstance(values.get(field), str) or not values[field].strip():
            raise ValueError(f"Specify {field} in --config or on the command line")
    if "vision_bundle" in values and "vision_model" in values:
        raise ValueError("choose an official vision model or a custom bundle, not both")
    model_field = "vision_bundle" if "vision_bundle" in values else "vision_model"
    if model_field == "vision_model":
        values.setdefault("vision_model", str(default_model_path("nano")))
    values.setdefault("port", 8080)
    if type(values["port"]) is not int or not 1 <= values["port"] <= 65535:
        raise ValueError("HTTP port must be in 1..65535")
    if not isinstance(values[model_field], str) or not values[model_field].strip():
        raise ValueError(f"{model_field} must be a file path")
    for field in ("camera_device", "radio_port", "profile", model_field):
        values[field] = str(Path(values[field]).expanduser().absolute())
    return values


def prepare(values, *, root=ROOT):
    from argos.backends.edgetx_profile import validate_profile
    from argos.backends.edgetx_yaw_stream import HELLO
    camera = str(Path(values["camera_device"]).resolve())
    if not re.fullmatch(r"/dev/video[0-9]+", camera):
        raise ValueError("Camera must resolve to /dev/videoN; reconnect it if the stable link is missing")
    if not values["radio_port"].startswith("/dev/"):
        raise ValueError("Pocket must be an explicit /dev/ serial device")
    profile = validate_profile(Path(values["profile"]).read_bytes())
    if values.get("vision_bundle") is not None:
        if values.get("vision_model") is not None:
            raise ValueError("choose an official vision model or a custom bundle, not both")
        detector = read_model_bundle(values["vision_bundle"]).identity()
    else:
        read_verified_model(values["vision_model"], variant="nano")
        detector = {"variant": "nano", "sha256": get_model_spec("nano").sha256}
    detector.update(threads=4, max_hz=10)
    identity = software_identity(root)
    manifest = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "software": identity, "settings": dict(values), "resolved_camera": camera,
                "detector": detector,
                "radio_profile": profile, "radio_protocol": HELLO.decode(),
                "radio_script_sha256": _digest(Path(root) / "scripts/edgetx/ArgFly.lua"),
                "radio_installation": "Local artifact verified; SD readback and receiver bench pending"}
    config = ConsoleConfig(environment="real", video_source="device", video_endpoint=camera,
                           vision_model=Path(values["vision_model"]) if values.get("vision_model") else None,
                           vision_bundle=Path(values["vision_bundle"]) if values.get("vision_bundle") else None,
                           vision_variant="nano",
                           vision_threads=4, vision_hz=10, yaw_assist=True)
    return config, manifest


@contextmanager
def instance(directory, port):
    """Bind HTTP before starting hardware workers; never kill an existing server."""
    import fcntl
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "instance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("ARGOS FLY is already running from this state directory") from None
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            yield listener


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="saved local JSON configuration")
    parser.add_argument("--camera-device", help="capture node or /dev/v4l/by-id/ link")
    parser.add_argument("--radio-port", help="Pocket /dev/serial/by-id/ link")
    parser.add_argument("--profile", type=Path, help="verified local ARGOS FLY model YAML")
    models = parser.add_mutually_exclusive_group()
    models.add_argument("--vision-model", type=Path, help="verified YOLOX-Nano ONNX (default: cache)")
    models.add_argument("--vision-bundle", type=Path, help="explicit custom YOLOX-Nano bundle")
    parser.add_argument("--port", type=int)
    parser.add_argument("--check", action="store_true", help="verify files and print identity; open no device")
    parser.add_argument("--save-config", type=Path, help="save settings for the next one-command launch")
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local/state/argos/fly",
                        help="local manifest, status and event log directory")
    args = parser.parse_args(argv)
    try:
        values = load_settings(args)
        config, manifest = prepare(values)
        if args.save_config is not None:
            destination = args.save_config.expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x") as output:
                json.dump(values, output, indent=2)
                output.write("\n")
        if args.check:
            print(json.dumps(manifest, indent=2))
            return 0
        import uvicorn
        from argos.console.app import create_app
        from argos.console.yaw_assist import YawAssistService
        directory = args.state_dir.expanduser()
        with instance(directory, values["port"]) as listener:
            run_directory = directory / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            run_directory.mkdir(parents=True)
            (run_directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            (directory / "latest.json").write_text(json.dumps({"directory": str(run_directory)}) + "\n")
            service = YawAssistService(values["radio_port"], values["port"],
                                      manifest={"git_revision": manifest["software"]["git_revision"],
                                                "git_dirty": manifest["software"]["git_dirty"],
                                                "runtime_sha256": manifest["software"]["runtime_sha256"],
                                                "detector": manifest["detector"],
                                                "radio_protocol": manifest["radio_protocol"],
                                                "radio_script_sha256": manifest["radio_script_sha256"]},
                                      directory=run_directory)
            app = create_app(config, yaw_service=service)
            print(f"ARGOS FLY · http://127.0.0.1:{values['port']} · SC middle then SC↑", flush=True)
            print(f"Git {manifest['software']['git_revision']} · dirty={manifest['software']['git_dirty']}", flush=True)
            uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=values["port"],
                                          access_log=False)).run(sockets=[listener])
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
