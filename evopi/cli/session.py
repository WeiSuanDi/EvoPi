"""Read-only CLI commands for persisted EvoPi Sessions."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from evopi.session import (
    CheckpointGCReport,
    CheckpointGCSettings,
    SessionError,
    SessionManager,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


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


def build_session_gc_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evopi session gc",
        description="Plan or apply derived Session Checkpoint garbage collection",
    )
    parser.add_argument("session", metavar="SESSION_ID|PATH")
    parser.add_argument("--session-root", type=Path)
    parser.add_argument("--keep-per-leaf", type=_positive_int, default=3)
    parser.add_argument("--protect-days", type=_non_negative_int, default=7)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def session_gc_main(argv: Sequence[str]) -> int:
    args = build_session_gc_parser().parse_args(list(argv))
    manager: SessionManager | None = None
    try:
        manager = SessionManager.open_for_maintenance(
            args.session,
            root=args.session_root,
        )
        plan = manager.plan_checkpoint_gc(
            CheckpointGCSettings(
                keep_per_leaf=args.keep_per_leaf,
                protect_days=args.protect_days,
            )
        )
        report = (
            manager.apply_checkpoint_gc(plan)
            if args.apply
            else CheckpointGCReport.preview(plan)
        )
    except (OSError, SessionError, ValueError) as exc:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "applied": bool(args.apply),
                        "passed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            print(f"EvoPi Session GC error: {exc}", file=sys.stderr)
        return 1
    finally:
        if manager is not None:
            manager.close()

    if args.json_output:
        print(
            json.dumps(
                report.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    else:
        mode = "Applied" if report.applied else "Dry run"
        print(f"{mode}: {report.session_id}")
        print(f"  Kept: {report.kept_count}")
        print(f"  Protected: {report.protected_count}")
        print(f"  Missing: {report.missing_count}")
        print(f"  Candidates: {report.candidate_count}")
        print(f"  Estimated bytes: {report.estimated_bytes}")
        print(f"  Deleted: {report.deleted_count}")
        print(f"  Reclaimed bytes: {report.reclaimed_bytes}")
        for error in report.errors:
            print(
                f"  Error: {error.relative_path}: {error.error}",
                file=sys.stderr,
            )
    return 0 if report.passed else 1


def print_session_opened(manager: SessionManager) -> None:
    info = manager.recovery_info
    print(
        f"EvoPi session {manager.session_id} ({info.reason})",
        file=sys.stderr,
    )
    for warning in info.warnings:
        print(f"EvoPi session warning: {warning}", file=sys.stderr)


__all__ = [
    "build_session_gc_parser",
    "build_session_list_parser",
    "print_session_opened",
    "session_gc_main",
    "session_list_main",
]
