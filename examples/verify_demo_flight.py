"""Verify the shipped flight using the same read-only archives as the console."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys

from PIL import Image

from argos.console.archive import RecordingArchive
from argos.console.visual_recording import VisualArchive


DEFAULT_DIRECTORY = Path(__file__).resolve().parent / "demo-flight"


def _require(condition, detail):
    if not condition:
        raise ValueError(detail)


def verify_demo_flight(directory=DEFAULT_DIRECTORY):
    """Read both archives, decode every JPEG and verify the chapter evidence.

    This opens no camera, MAVLink transport, model or simulation. The existing
    archive readers validate completion, capture binding, chronology, matched
    detections, and each image's stored digest and decoded dimensions.
    """
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    _require(manifest.get("format") == "argos.demo-flight" and manifest.get("version") == 1,
             "Unsupported demo manifest")
    identifier = manifest["id"]
    _require(isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier),
             "Invalid demo identifier")
    for role, suffix in (("telemetry", ".jsonl"), ("visual", ".visual.sqlite3")):
        entry = manifest["files"][role]
        _require(entry["path"] == identifier + suffix, "Demo filenames must match the recording ID")
        path = directory / entry["path"]
        _require(path.is_file() and not path.is_symlink(), "Demo assets must be regular files")
        _require(path.stat().st_size == entry["size_bytes"], f"{role} size differs from the manifest")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        _require(digest == entry["sha256"], f"{role} SHA-256 differs from the manifest")

    telemetry = RecordingArchive(directory).metadata(identifier)
    context = telemetry["context"]
    capture, counts = manifest["capture"], manifest["counts"]
    _require(context is not None, "Demo capture context is missing")
    for key in ("started_at", "ended_at", "duration_s"):
        _require(telemetry[key] == capture[key], f"Capture {key} differs from the archive")
    _require(context["run_id"] == capture["run_id"], "Capture run ID differs from the archive")
    _require(context["captured_at_utc"] == capture["captured_at_utc"], "Capture date differs from the archive")
    _require(context["configuration"]["environment"] == capture["environment"] == "simulation",
             "The public demo must be identified as a simulation")
    _require(telemetry["events"] == counts["telemetry_events"], "Telemetry count differs from the manifest")

    archive = VisualArchive(directory)
    binding = {"started_at": telemetry["started_at"], "run_id": context["run_id"]}
    visual = archive.metadata(identifier, **binding)
    _require(visual["state"] == "complete", "Demo visual recording is incomplete")
    _require(visual["revision"] == manifest["files"]["visual"]["sha256"], "Visual revision mismatch")
    for key in ("started_at", "ended_at", "duration_s"):
        _require(visual[key] == capture[key], f"Visual {key} differs from the capture")
    for key in ("frames", "samples", "events", "dropped"):
        _require(visual[key] == counts[f"visual_{key}"], f"Visual {key} count differs from the manifest")
    access = {**binding, "revision": visual["revision"]}
    for index in range(visual["frames"]):
        # frame() performs the digest check and full JPEG decode. Opening the
        # returned image here only compares the fixture's declared resolution.
        jpeg = archive.frame(identifier, index, **access)
        with Image.open(io.BytesIO(jpeg)) as image:
            _require(image.size == (capture["image_width"], capture["image_height"]),
                     f"Frame {index} differs from the manifest resolution")

    final = archive.replay(identifier, capture["duration_s"], **access)
    _require(final["events_count"] <= final["events_limit"], "Demo chapters require a complete event list")
    events = {event["index"]: event for event in final["events"]}
    previous = -1.
    for chapter in manifest["chapters"]:
        at, evidence = chapter["at_s"], chapter["evidence"]
        _require(previous <= at <= capture["duration_s"], "Demo chapters are out of order")
        _require(evidence["source"] == "visual_event", "Unsupported chapter evidence")
        event = events.get(evidence["index"])
        _require(event is not None and event["at_s"] == at, "Chapter time does not match its event")
        _require(all(event[key] == evidence[key] for key in ("kind", "detail", "status")),
                 "Chapter description does not match its recorded evidence")
        previous = at
    return {"id": identifier, "duration_s": capture["duration_s"], **counts,
            "chapters": len(manifest["chapters"]), "integrity": "verified"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    args = parser.parse_args(argv)
    try:
        result = verify_demo_flight(args.directory)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Demo verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
