"""Digest-bound Plugin lifecycle commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from evopi.evolution import (
    ActivationDecision,
    ActivationStore,
    WorkspaceTrustStore,
)
from evopi.plugins import (
    PluginArtifactStore,
    PluginCandidateError,
    PluginReviewReport,
    available_plugin_templates,
    approved_plugin_entrypoints,
    initialize_plugin_candidate,
    resolve_evopi_home,
    review_plugin,
)


def plugin_main(action: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"evopi plugin {action}")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--operator", default="local-user")
    parser.add_argument("--reason")
    parser.add_argument("--trust-workspace", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--template",
        choices=sorted(available_plugin_templates()),
        default="basic",
    )
    parser.add_argument("--path", type=Path)
    args = parser.parse_args(argv)
    args._action = action
    try:
        if action == "review":
            return _review(args)
        if action == "init":
            return _init(args)
        if action == "examples":
            return _examples(args)
        if action in {"approve", "install"}:
            return _approve(args)
        if action == "deny":
            return _deny(args)
        if action == "remove":
            return _remove(args)
        if action == "list":
            return _list(args)
        if action == "reload":
            return _reload(args)
    except (OSError, ValueError, PluginCandidateError) as exc:
        print(f"Plugin error: {exc}", file=sys.stderr)
        return 1
    print(f"Unknown plugin action: {action}", file=sys.stderr)
    return 1


def _init(args: argparse.Namespace) -> int:
    name = args.target
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Plugin name is required")
    target = (
        args.path
        if args.path is not None
        else args.workspace / ".evopi" / "plugin-candidates" / name
    )
    candidate = initialize_plugin_candidate(
        name,
        template=args.template,
        path=target,
    )
    report = review_plugin(candidate)
    _print(
        {
            "status": "candidate",
            "name": report.candidate.name,
            "template": args.template,
            "path": str(candidate),
            "digest": report.candidate.artifact.digest,
            "next": "review -> approve -> reload",
        },
        json_output=args.json,
    )
    return 0


def _examples(args: argparse.Namespace) -> int:
    templates = available_plugin_templates()
    _print(
        {
            "templates": [
                {
                    "name": template.name,
                    "description": template.description,
                }
                for template in templates.values()
            ]
        },
        json_output=args.json,
    )
    return 0


def _review(args: argparse.Namespace) -> int:
    path = _required_path(args.target)
    report = review_plugin(path)
    payload = _report_payload(report)
    _print(payload, json_output=args.json)
    return 0 if report.passed else 1


def _approve(args: argparse.Namespace) -> int:
    if args._action == "install":
        print(
            "Warning: 'plugin install' is a deprecated alias; "
            "use review then approve.",
            file=sys.stderr,
        )
    path = _required_path(args.target)
    report = review_plugin(path)
    if not report.passed:
        _print(_report_payload(report), json_output=args.json)
        return 1
    home = resolve_evopi_home()
    store = ActivationStore(home / "activations.json")
    snapshots = PluginArtifactStore(home / "artifacts" / "plugins")
    snapshot = snapshots.install(report.candidate)
    record = store.add(
        candidate=report.candidate.artifact,
        decision=ActivationDecision.APPROVED,
        decided_by=args.operator,
        evidence=("plugin-review-v1",),
        reason=args.reason,
    )
    if args.trust_workspace:
        WorkspaceTrustStore(home / "workspace-trust.json").trust(
            args.workspace,
            trusted_by=args.operator,
        )
    payload = {
        "status": "approved",
        "record_id": record.record_id,
        "name": report.candidate.name,
        "version": report.candidate.manifest.version,
        "digest": report.candidate.artifact.digest,
        "snapshot": str(snapshot),
        "workspace_trusted": bool(args.trust_workspace),
    }
    _print(payload, json_output=args.json)
    return 0


def _deny(args: argparse.Namespace) -> int:
    report = review_plugin(_required_path(args.target))
    home = resolve_evopi_home()
    record = ActivationStore(home / "activations.json").add(
        candidate=report.candidate.artifact,
        decision=ActivationDecision.DENIED,
        decided_by=args.operator,
        evidence=("plugin-review-v1",),
        reason=args.reason,
    )
    _print(
        {
            "status": "denied",
            "record_id": record.record_id,
            "name": report.candidate.name,
            "digest": report.candidate.artifact.digest,
        },
        json_output=args.json,
    )
    return 0


def _remove(args: argparse.Namespace) -> int:
    name = args.target
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Plugin name is required")
    home = resolve_evopi_home()
    store = ActivationStore(home / "activations.json")
    latest = next(
        (
            record
            for record in reversed(store.records())
            if record.candidate.kind == "plugin" and record.candidate.name == name
        ),
        None,
    )
    if latest is None:
        raise ValueError(f"Plugin '{name}' has no activation record")
    record = store.add(
        candidate=latest.candidate,
        decision=ActivationDecision.DENIED,
        decided_by=args.operator,
        reason=args.reason or "removed by user",
    )
    _print(
        {"status": "denied", "record_id": record.record_id, "name": name},
        json_output=args.json,
    )
    return 0


def _list(args: argparse.Namespace) -> int:
    home = resolve_evopi_home()
    path = home / "activations.json"
    records = ActivationStore(path).records() if path.exists() else ()
    latest = {}
    for record in records:
        if record.candidate.kind == "plugin":
            latest[record.candidate.name] = record
    active = set(approved_plugin_entrypoints(args.workspace, home=home))
    artifacts = PluginArtifactStore(home / "artifacts" / "plugins")
    items = []
    for name, record in sorted(latest.items()):
        snapshot = artifacts.path_for(record.candidate.digest)
        approved = record.decision is ActivationDecision.APPROVED
        status = "approved" if approved else "denied"
        try:
            entrypoint = artifacts.entrypoint_for(record.candidate)
        except PluginCandidateError:
            entrypoint = None
            if approved:
                status = "failed"
        if entrypoint in active:
            status = "active"
        items.append(
            {
                "name": name,
                "version": record.candidate.version,
                "digest": record.candidate.digest,
                "status": status,
                "snapshot": str(snapshot),
            }
        )
    _print({"plugins": items}, json_output=args.json)
    return 0


def _reload(args: argparse.Namespace) -> int:
    active = approved_plugin_entrypoints(
        args.workspace,
        home=resolve_evopi_home(),
    )
    _print(
        {
            "status": "validated",
            "active_snapshots": [str(path) for path in active],
            "note": "running Harnesses apply this set through transactional /reload",
        },
        json_output=args.json,
    )
    return 0


def _required_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Plugin candidate path is required")
    return Path(value).expanduser().resolve()


def _report_payload(report: PluginReviewReport) -> dict[str, object]:
    candidate = report.candidate
    return {
        "status": "reviewed" if report.passed else "failed",
        "name": candidate.name,
        "version": candidate.manifest.version,
        "digest": candidate.artifact.digest,
        "errors": list(report.errors),
        "warnings": list(report.warnings),
    }


def _print(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if "plugins" in payload:
        items = cast(list[dict[str, Any]], payload["plugins"])
        if not items:
            print("No governed plugins.")
            return
        for item in items:
            print(
                f"{item['name']} {item['version']} "
                f"[{item['status']}] {item['digest'][:12]}"
            )
        return
    if "templates" in payload:
        items = cast(list[dict[str, Any]], payload["templates"])
        for item in items:
            print(f"{item['name']}: {item['description']}")
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


__all__ = ["plugin_main"]
