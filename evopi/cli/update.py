"""Explicit GitHub Release update command for managed EvoPi runtimes."""

from __future__ import annotations

import argparse
import json
import os
import sys

from evopi import __version__
from evopi.configuration import resolve_user_config_home
from evopi.distribution import (
    DistributionError,
    GitHubReleaseClient,
    ManagedRuntime,
    UpdateResult,
    UpdateStatus,
    version_key,
)


def build_update_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evopi update",
        description="Check or update EvoPi from official GitHub Releases",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="Only check for an update")
    actions.add_argument("--rollback", action="store_true", help="Switch to the previous runtime")
    parser.add_argument("--yes", action="store_true", help="Approve the explicit update action")
    parser.add_argument(
        "--enable-feature",
        action="append",
        choices=("remote",),
        default=[],
        metavar="FEATURE",
        help="Install and preserve an optional managed-runtime feature",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _confirmation(prompt: str) -> bool:
    print(f"{prompt} [y/N]: ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


def _unsupported_result(current: str) -> UpdateResult:
    if os.getenv("CONDA_PREFIX"):
        hint = "This Conda install is externally managed; update the environment/package instead."
    elif os.getenv("PIPX_HOME"):
        hint = "This pipx install is externally managed; use 'pipx upgrade evopi'."
    else:
        hint = "This pip/editable install is externally managed; reinstall it with your package tool."
    return UpdateResult(
        status=UpdateStatus.UNSUPPORTED_INSTALL,
        current_version=current,
        message=hint,
    )


def _emit(result: UpdateResult, *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
    else:
        stream = sys.stderr if result.status is UpdateStatus.FAILED else sys.stdout
        print(result.message, file=stream)
        for warning in result.warnings:
            print(f"Warning: {warning}", file=sys.stderr)
    if result.status in {
        UpdateStatus.UP_TO_DATE,
        UpdateStatus.UPDATE_AVAILABLE,
        UpdateStatus.UPDATED,
        UpdateStatus.ROLLED_BACK,
    }:
        return 0
    if result.status in {UpdateStatus.DECLINED, UpdateStatus.UNSUPPORTED_INSTALL}:
        return 2
    return 1


def update_main(argv: list[str]) -> int:
    try:
        args = build_update_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    home = resolve_user_config_home()
    runtime = ManagedRuntime(home)
    requested_features = tuple(sorted({*runtime.current_features, *args.enable_feature}))
    if args.rollback:
        if not runtime.is_managed_process:
            return _emit(_unsupported_result(__version__), json_output=args.json_output)
        if not args.yes and (not sys.stdin.isatty() or not _confirmation("Roll back EvoPi?")):
            return _emit(
                UpdateResult(
                    status=UpdateStatus.DECLINED,
                    current_version=runtime.current_version,
                    message="rollback declined",
                ),
                json_output=args.json_output,
            )
        return _emit(runtime.rollback(), json_output=args.json_output)

    client = GitHubReleaseClient()
    try:
        info = client.latest_info()
        current = (
            runtime.current_version or __version__
            if runtime.is_managed_process
            else __version__
        )
        try:
            available = version_key(info.version) > version_key(current)
        except DistributionError:
            available = info.version != current
        missing_features = sorted(set(requested_features) - set(runtime.current_features))
        if not available and not missing_features:
            return _emit(
                UpdateResult(
                    status=UpdateStatus.UP_TO_DATE,
                    current_version=current,
                    target_version=info.version,
                    release_url=info.release_url,
                    message=f"EvoPi {current} is up to date.",
                ),
                json_output=args.json_output,
            )
        if args.check:
            return _emit(
                UpdateResult(
                    status=UpdateStatus.UPDATE_AVAILABLE,
                    current_version=current,
                    target_version=info.version,
                    release_url=info.release_url,
                    message=(
                        f"EvoPi {info.version} is available: {info.release_url}"
                        if available
                        else f"Feature(s) {', '.join(missing_features)} are available."
                    ),
                ),
                json_output=args.json_output,
            )
        if not runtime.is_managed_process:
            return _emit(_unsupported_result(current), json_output=args.json_output)
        if not args.yes:
            if not sys.stdin.isatty():
                return _emit(
                    UpdateResult(
                        status=UpdateStatus.DECLINED,
                        current_version=current,
                        target_version=info.version,
                        release_url=info.release_url,
                        message="non-interactive update requires --yes",
                    ),
                    json_output=args.json_output,
                )
            action = (
                f"Update EvoPi {current} to {info.version}? {info.release_url}"
                if available
                else f"Enable feature(s) {', '.join(missing_features)} for EvoPi {current}?"
            )
            if not _confirmation(action):
                return _emit(
                    UpdateResult(
                        status=UpdateStatus.DECLINED,
                        current_version=current,
                        target_version=info.version,
                        release_url=info.release_url,
                        message="update declined",
                    ),
                    json_output=args.json_output,
                )
        wheel = client.download(info)
        return _emit(
            runtime.install(info, wheel, features=requested_features),
            json_output=args.json_output,
        )
    except DistributionError as exc:
        return _emit(
            UpdateResult(
                status=UpdateStatus.FAILED,
                current_version=runtime.current_version or __version__,
                message=f"update failed: {exc}",
            ),
            json_output=args.json_output,
        )
    finally:
        client.close()


__all__ = ["build_update_parser", "update_main"]
