#!/usr/bin/env python3
"""Freeze and replay explicit synthetic multi-person selection-layer controls."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from argos.harness.multi_person_scenarios import freeze_manifest, qualify, read_manifest, write_viewer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-manifest", type=Path)
    parser.add_argument("--scenario-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.freeze_manifest:
        if args.scenario_manifest or args.output_dir:
            parser.error("Freeze is a separate step before qualification")
        print(freeze_manifest(args.freeze_manifest))
        return
    if not args.scenario_manifest or not args.output_dir:
        parser.error("Qualification needs --scenario-manifest and --output-dir")
    manifest, sha = read_manifest(args.scenario_manifest)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    source_paths = (
        "argos/harness/multi_person_scenarios.py", "examples/qualify_multi_person.py",
        "argos/perception/candidate_recovery.py", "argos/perception/recovery_prototype.py",
        "argos/console/yaw_preview.py", "argos/backends/yaw_stream_source.py",
        "argos/backends/vision_bench_source.py", "argos/perception/appearance.py",
        "argos/perception/image_tracks.py", "argos/perception/static/multi_person_qualification.html",
    )
    root = Path(__file__).resolve().parents[1]
    source_hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in source_paths}
    started_at = datetime.now(timezone.utc).isoformat()
    (args.output_dir / "implementation-freeze.json").write_text(json.dumps(dict(
        started_at=started_at, manifest_file_sha256=sha, implementation_sha256=source_hashes), indent=2) + "\n")
    report = qualify(manifest)
    if any(hashlib.sha256((root / name).read_bytes()).hexdigest() != value
           for name, value in source_hashes.items()):
        raise RuntimeError("Implementation source changed during qualification")
    report.update(started_at=started_at, implementation_sha256=source_hashes,
                  implementation_unchanged_after_execution=True)
    if hashlib.sha256(args.scenario_manifest.read_bytes()).hexdigest() != sha:
        raise RuntimeError("Frozen manifest changed during execution")
    report.update(manifest_file_sha256=sha, generated_at=datetime.now(timezone.utc).isoformat())
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    write_viewer(args.output_dir / "index.html", report, manifest)
    print(json.dumps(dict(status=report["status"], scenarios=len(manifest["scenarios"]),
                         totals=report["totals"]), indent=2))


if __name__ == "__main__":
    main()
