"""Read-only CLI commands for persisted EvoPi Sessions."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from evopi.session import SessionManager


def build_session_list_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evopi session list",
        description="List persisted EvoPi Sessions",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--all", action="store_true", dest="all_workspaces")
    parser.add_argument("--session-root", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def session_list_main(argv: Sequence[str]) -> int:
    args = build_session_list_parser().parse_args(list(argv))
    summaries = SessionManager.list(
        workspace=None if args.all_workspaces else args.workspace,
        root=args.session_root,
    )
    if args.json_output:
        print(
            json.dumps(
                [summary.to_dict() for summary in summaries],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0
    if not summaries:
        print("No EvoPi Sessions found.")
        return 0
    for summary in summaries:
        status = summary.error or summary.last_run_reason or "empty"
        print(
            f"{summary.session_id}  {summary.updated_at.isoformat()}  "
            f"{status}  {summary.workspace}"
        )
        print(f"  {summary.path}")
    return 0


def print_session_opened(manager: SessionManager) -> None:
    info = manager.recovery_info
    print(
        f"EvoPi session {manager.session_id} ({info.reason})",
        file=sys.stderr,
    )
    for warning in info.warnings:
        print(f"EvoPi session warning: {warning}", file=sys.stderr)


__all__ = [
    "build_session_list_parser",
    "print_session_opened",
    "session_list_main",
]
