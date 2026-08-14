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
    subparsers.add_parser(
        "serve",
        help="Serve one secure WSS Remote Gateway",
        add_help=False,
    )
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


def build_remote_serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evopi remote serve",
        description="Serve one trusted CodingHarness through the secure Remote Gateway",
        epilog="Model, Session, Tool, Policy and resource options accepted by 'evopi rpc' may follow.",
        allow_abbrev=False,
    )
    parser.add_argument("name", help="Initialized Remote Host profile name")
    parser.add_argument("--remote-root", type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--proxy", action="store_true", dest="proxy_mode")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--trusted-proxy", action="append", dest="trusted_proxies")
    parser.add_argument("--allowed-host", action="append", dest="allowed_hosts")
    parser.add_argument("--allowed-origin", action="append", dest="allowed_origins")
    parser.add_argument("--console", action="store_true")
    parser.add_argument("--max-connections", type=int, default=64)
    parser.add_argument("--max-connections-per-ip", type=int, default=8)
    parser.add_argument("--max-connections-per-device", type=int, default=4)
    parser.add_argument("--max-outbound-items", type=int, default=128)
    parser.add_argument("--max-outbound-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--handshake-rate", type=int, default=10)
    parser.add_argument("--pairing-rate", type=int, default=5)
    parser.add_argument("--request-rate", type=int, default=120)
    parser.add_argument("--run-rate", type=int, default=6)
    parser.add_argument("--confirmation-rate", type=int, default=30)
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


__all__ = ["build_remote_parser", "build_remote_serve_parser", "remote_main"]
