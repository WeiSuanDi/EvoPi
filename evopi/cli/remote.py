"""Local Remote Host management commands."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from evopi.configuration import resolve_user_config_home
from evopi.remote import RemoteError, RemoteHostConfig, RemoteHostStore


def build_remote_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evopi remote")
    subparsers = parser.add_subparsers(dest="action", required=True)
    init = subparsers.add_parser("init", help="Create one local Remote Host profile")
    init.add_argument("name")
    init.add_argument("--workspace", type=Path, default=Path.cwd())
    init.add_argument("--profile", default="default")
    init.add_argument("--remote-root", type=Path)
    init.add_argument("--json", action="store_true", dest="json_output")
    return parser


def remote_main(argv: Sequence[str]) -> int:
    args = build_remote_parser().parse_args(list(argv))
    root = args.remote_root or resolve_user_config_home() / "remote"
    try:
        if args.action == "init":
            workspace = args.workspace.expanduser().resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            config = RemoteHostStore(root).initialize(
                RemoteHostConfig(
                    name=args.name,
                    workspace=workspace,
                    model_profile=args.profile,
                )
            )
            payload = {
                "schema_version": 1,
                "host_id": config.host_id,
                "name": config.name,
                "workspace": str(config.workspace),
                "model_profile": config.model_profile,
            }
            if args.json_output:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                print(f"Remote Host initialized: {config.name}")
                print(f"Host ID: {config.host_id}")
            return 0
    except (OSError, RemoteError) as exc:
        print(f"EvoPi remote error: {exc}", file=sys.stderr)
        return 1
    return 2


__all__ = ["build_remote_parser", "remote_main"]
