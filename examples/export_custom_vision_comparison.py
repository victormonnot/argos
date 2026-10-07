#!/usr/bin/env python3
"""Export saved Nano/custom comparison to one offline HTML file; no inference."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argos.perception.custom_comparison_export import export_comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = export_comparison(args.comparison_dir, args.output_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(f"Saved {result['frames']} original frames: {args.output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
