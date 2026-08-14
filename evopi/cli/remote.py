"""Local Remote Host management commands."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from evopi.configuration import resolve_user_config_home
from evopi.remote import (
    RemoteAdminClient,
    RemoteAdminRequest,
    RemoteError,
    RemoteHostConfig,
    RemoteHostStore,
    resolve_admin_endpoint,
)


def build_remote_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evopi remote")
    subparsers = parser.add_subparsers(dest="action", required=True)
    init = subparsers.add_parser("init", help="Create one local Remote Host profile")
    init.add_argument("name")
    init.add_argument("--workspace", type=Path, default=Path.cwd())
    init.add_argument("--profile", default="default")
    init.add_argument("--remote-root", type=Path)
    init.add_argument("--json", action="store_true", dest="json_output")
    for action, help_text in (
        ("pair", "Issue a one-time pairing code through the running Host"),
        ("status", "Show the running Host security status"),
    ):
        command = subparsers.add_parser(action, help=help_text)
        command.add_argument("name")
        command.add_argument("--remote-root", type=Path)
        command.add_argument("--json", action="store_true", dest="json_output")

    requests = subparsers.add_parser("requests", help="Manage pending device requests")
    request_actions = requests.add_subparsers(dest="request_action", required=True)
    request_list = request_actions.add_parser("list")
    request_list.add_argument("name")
    request_approve = request_actions.add_parser("approve")
    request_approve.add_argument("name")
    request_approve.add_argument("request_id")
    request_approve.add_argument(
        "--scope", action="append", choices=["observe", "control", "confirm"], required=True
    )
    request_deny = request_actions.add_parser("deny")
    request_deny.add_argument("name")
    request_deny.add_argument("request_id")
    for command in (request_list, request_approve, request_deny):
        command.add_argument("--remote-root", type=Path)
        command.add_argument("--json", action="store_true", dest="json_output")

    devices = subparsers.add_parser("devices", help="Manage approved devices")
    device_actions = devices.add_subparsers(dest="device_action", required=True)
    device_list = device_actions.add_parser("list")
    device_list.add_argument("name")
    device_scopes = device_actions.add_parser("scopes")
    device_scopes.add_argument("name")
    device_scopes.add_argument("device_id")
    device_scopes.add_argument(
        "--scope", action="append", choices=["observe", "control", "confirm"], required=True
    )
    device_revoke = device_actions.add_parser("revoke")
    device_revoke.add_argument("name")
    device_revoke.add_argument("device_id")
    for command in (device_list, device_scopes, device_revoke):
        command.add_argument("--remote-root", type=Path)
        command.add_argument("--json", action="store_true", dest="json_output")
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
        method, params = _admin_operation(args)
        result = _call_admin(RemoteHostStore(root), args.name, method, params)
        if args.json_output:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_admin_result(method, result)
        return 0
    except (OSError, RemoteError) as exc:
        print(f"EvoPi remote error: {exc}", file=sys.stderr)
        return 1
    return 2


def _admin_operation(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    if args.action == "pair":
        return "pair.issue", {}
    if args.action == "status":
        return "status", {}
    if args.action == "requests":
        if args.request_action == "list":
            return "requests.list", {}
        if args.request_action == "approve":
            return "requests.approve", {
                "request_id": args.request_id,
                "scopes": args.scope,
            }
        return "requests.deny", {"request_id": args.request_id}
    if args.device_action == "list":
        return "devices.list", {}
    if args.device_action == "scopes":
        return "devices.scopes", {
            "device_id": args.device_id,
            "scopes": args.scope,
        }
    return "devices.revoke", {"device_id": args.device_id}


def _call_admin(
    store: RemoteHostStore,
    host_name: str,
    method: str,
    params: dict[str, object],
) -> dict[str, object]:
    config = store.load_config(host_name)
    endpoint = resolve_admin_endpoint(config.host_id, store.host_path(host_name))
    client = RemoteAdminClient(endpoint, store.load_management_secret(host_name))
    response = client.call(
        RemoteAdminRequest(
            request_id=uuid4().hex,
            method=method,
            params=params,
        )
    )
    if not response.ok or response.result is None:
        raise RemoteError(response.error or "Remote Host rejected the operation")
    return dict(response.result)


def _print_admin_result(method: str, result: dict[str, object]) -> None:
    if method == "pair.issue":
        print(f"Pairing code: {result['code']}")
        print(f"Expires at: {result['expires_at']}")
        return
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


__all__ = ["build_remote_parser", "remote_main"]
