"""Validate that a Git tag exactly matches the package version."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    if args.tag != f"v{version}":
        parser.error(f"tag {args.tag!r} does not match package version {version!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
