#!/usr/bin/env python3
"""Fetch the pinned Gazebo walking-person mesh before starting a simulation."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.request import urlopen


ASSET_URL = "https://fuel.gazebosim.org/1.0/Mingfei/models/actor/1/files/meshes/walk.dae"
ASSET_SHA256 = "49af0df3a319d1cb8ca2cebf02dbd00f625e5d5bec820bc5e109925b18b65c6e"
ASSET_BYTES = 2_277_322
ASSET_NOTICE = """Walking person mesh: walk.dae, unmodified.
Source: Mingfei / actor, Gazebo Fuel model version 1.
https://fuel.gazebosim.org/1.0/Mingfei/models/actor/1
https://fuel.gazebosim.org/1.0/Mingfei/models/actor/1/files/meshes/walk.dae
License declared by Fuel: Creative Commons Attribution 4.0 International.
https://creativecommons.org/licenses/by/4.0/
The ARGOS scene supplies its own trajectory; the original mesh is unchanged.
"""


def default_assets_dir() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME", "")
    root = Path(cache) if cache and Path(cache).is_absolute() else Path.home() / ".cache"
    return root / "argos" / "gazebo"


def verified_mesh(assets_dir: Path) -> Path:
    """Return a local checked mesh; never download implicitly during launch."""
    mesh = assets_dir / "argos_walking_person" / "walk.dae"
    try:
        with mesh.open("rb") as source:
            data = source.read(ASSET_BYTES + 1)
    except OSError as exc:
        raise ValueError("walking-person mesh is missing; run examples/setup_vision_scene.py first") from exc
    if len(data) != ASSET_BYTES or hashlib.sha256(data).hexdigest() != ASSET_SHA256:
        raise ValueError("walking-person mesh checksum differs; run examples/setup_vision_scene.py again")
    return mesh


def install(assets_dir: Path) -> Path:
    """Download exactly one bounded, digest-pinned file and replace atomically."""
    try:
        mesh = verified_mesh(assets_dir)
    except ValueError:
        pass
    else:
        (mesh.parent / "NOTICE.txt").write_text(ASSET_NOTICE, encoding="utf-8")
        return mesh
    with urlopen(ASSET_URL, timeout=30) as response:
        data = response.read(ASSET_BYTES + 1)
    if len(data) != ASSET_BYTES or hashlib.sha256(data).hexdigest() != ASSET_SHA256:
        raise ValueError("downloaded walking-person mesh failed its size/SHA-256 check")
    directory = assets_dir / "argos_walking_person"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".walk-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.replace(directory / "walk.dae")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    (directory / "NOTICE.txt").write_text(ASSET_NOTICE, encoding="utf-8")
    return verified_mesh(assets_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, default=default_assets_dir())
    args = parser.parse_args()
    try:
        mesh = install(args.assets_dir.expanduser().resolve())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Verified Gazebo walking person: {mesh}")
    print(f"SHA-256: {ASSET_SHA256}")


if __name__ == "__main__":
    main()
