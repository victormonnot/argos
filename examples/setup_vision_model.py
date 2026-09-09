#!/usr/bin/env python3
"""Download and verify an optional, pinned YOLOX model before starting ARGOS."""
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
    MODEL_CATALOG, default_model_path, get_model_spec, read_verified_model,
)


def model_notice(model) -> str:
    return f"""{model.label} ONNX, unmodified upstream release 0.1.1rc0.
Copyright Megvii, Inc. and its affiliates.
Source: {model.url}
Project license: Apache License, Version 2.0.
{MODEL_LICENSE_URL}
SHA-256: {model.sha256}
Input: {model.input_size} x {model.input_size}; variant: {model.variant}.
ARGOS performs person-class inference; these weights are not fine-tuned for its scene.
"""


MODEL_NOTICE = model_notice(get_model_spec())


def install(output: Path, *, variant: str = "tiny") -> Path:
    """Reuse a checked local file, or atomically install one checked download."""
    model = get_model_spec(variant)
    notice = model_notice(model)
    output = output.expanduser().resolve()
    try:
        read_verified_model(output, variant=variant)
    except ValueError:
        pass
    else:
        output.with_suffix(".NOTICE.txt").write_text(notice, encoding="utf-8")
        return output
    with urlopen(model.url, timeout=30) as response:
        data = response.read(model.size_bytes + 1)
    if len(data) != model.size_bytes or hashlib.sha256(data).hexdigest() != model.sha256:
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
    output.with_suffix(".NOTICE.txt").write_text(notice, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=tuple(MODEL_CATALOG), default="tiny",
                        help="official model export: tiny (416px, default) or s (640px)")
    parser.add_argument("--output", type=Path,
                        help="local ONNX destination (default: XDG user cache)")
    args = parser.parse_args()
    try:
        output = install(args.output or default_model_path(args.variant), variant=args.variant)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    model = get_model_spec(args.variant)
    print(f"Verified {model.label} person detector: {output}")
    print(f"SHA-256: {model.sha256}")


if __name__ == "__main__":
    main()
