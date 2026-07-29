"""CLI entrypoints for governed Policy candidate lifecycle commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from evopi.evolution import (
    initialize_policy_candidate,
    inspect_policy_candidate,
)


def policy_init_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="evopi policy init",
        description="Create one inactive Policy candidate directory",
    )
    parser.add_argument("name")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(list(argv))
    target = args.path or Path.cwd() / ".evopi" / "policy-candidates" / args.name
    try:
        path = initialize_policy_candidate(args.name, path=target)
        candidate = inspect_policy_candidate(path).candidate
    except (OSError, ValueError) as exc:
        print(f"EvoPi policy init error: {exc}", file=sys.stderr)
        return 1
    payload = {
        "status": "candidate",
        "name": candidate.manifest.name,
        "version": candidate.manifest.version,
        "path": str(path),
        "digest": candidate.artifact.digest,
        "next": "review",
    }
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


__all__ = ["policy_init_main"]
