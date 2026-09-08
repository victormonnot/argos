#!/usr/bin/env python3
"""Download and verify the optional YOLOX-Tiny model before starting ARGOS."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argos.perception.yolox import (
    MODEL_BYTES, MODEL_LICENSE_URL, MODEL_SHA256, MODEL_URL,
    default_model_path, read_verified_model,
)


MODEL_NOTICE = f"""YOLOX-Tiny ONNX, unmodified upstream release 0.1.1rc0.
Copyright Megvii, Inc. and its affiliates.
Source: {MODEL_URL}
Project license: Apache License, Version 2.0.
{MODEL_LICENSE_URL}
SHA-256: {MODEL_SHA256}
ARGOS performs person-class inference; these weights are not fine-tuned for its scene.
"""


def install(output: Path) -> Path:
    """Reuse a checked local file, or atomically install one checked download."""
    output = output.expanduser().resolve()
    try:
        read_verified_model(output)
    except ValueError:
        pass
    else:
        output.with_suffix(".NOTICE.txt").write_text(MODEL_NOTICE, encoding="utf-8")
        return output
    with urlopen(MODEL_URL, timeout=30) as response:
        data = response.read(MODEL_BYTES + 1)
    if len(data) != MODEL_BYTES or hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise ValueError("downloaded vision model failed its size/SHA-256 check")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".yolox-", delete=False) as target:
            temporary = Path(target.name)
            target.write(data)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    output.with_suffix(".NOTICE.txt").write_text(MODEL_NOTICE, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=default_model_path(),
                        help="local ONNX destination (default: XDG user cache)")
    args = parser.parse_args()
    try:
        output = install(args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Verified YOLOX-Tiny person detector: {output}")
    print(f"SHA-256: {MODEL_SHA256}")


if __name__ == "__main__":
    main()
