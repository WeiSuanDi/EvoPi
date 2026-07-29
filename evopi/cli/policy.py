"""CLI entrypoints for governed Policy candidate lifecycle commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence, cast

from evopi.evolution import (
    ActivationStore,
    ArtifactActivationError,
    PolicyActivationService,
    PolicyApprovalService,
    PolicyArtifactStore,
    PolicyEvidenceError,
    PolicyEvidenceStore,
    PolicyReplacement,
    PolicySelectionStore,
    initialize_policy_candidate,
    inspect_policy_candidate,
    resolve_evolution_home,
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


def policy_lifecycle_main(action: str, argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"evopi policy {action}")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--operator", default="local-user")
    parser.add_argument("--reason")
    parser.add_argument("--accept-findings", action="store_true")
    parser.add_argument("--replace", metavar="POLICY_NAME")
    parser.add_argument("--expected-digest", metavar="SHA256")
    parser.add_argument("--to", metavar="APPROVAL_ID")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(list(argv))
    try:
        services = _policy_services()
        if action == "approve":
            payload = _approve(args, services)
        elif action == "deny":
            payload = _deny(args, services)
        elif action == "activate":
            payload = _activate(args, services)
        elif action == "deactivate":
            payload = _deactivate(args, services)
        elif action == "rollback":
            payload = _rollback(args, services)
        elif action in {"list", "status"}:
            payload = _status(args, services)
        else:
            raise ValueError(f"unknown Policy lifecycle action: {action}")
    except (OSError, ValueError, ArtifactActivationError, PolicyEvidenceError) as exc:
        print(f"EvoPi policy {action} error: {exc}", file=sys.stderr)
        return 1
    _print(payload, json_output=args.json_output)
    return 0


def _policy_services() -> tuple[
    PolicyEvidenceStore,
    ActivationStore,
    PolicyArtifactStore,
    PolicySelectionStore,
    PolicyApprovalService,
    PolicyActivationService,
]:
    home = resolve_evolution_home()
    evidence = PolicyEvidenceStore(home / "reviews" / "policies")
    activations = ActivationStore(home / "activations.json")
    artifacts = PolicyArtifactStore(home / "artifacts" / "policies")
    selections = PolicySelectionStore(home / "policy-selections.json")
    approvals = PolicyApprovalService(evidence, activations, artifacts)
    runtime = PolicyActivationService(activations, artifacts, selections)
    return evidence, activations, artifacts, selections, approvals, runtime


def _approve(args: argparse.Namespace, services: tuple) -> dict[str, object]:
    target = _target(args)
    evidence_store, _, _, _, approvals, _ = services
    evidence = evidence_store.load(target)
    record = approvals.approve(
        evidence,
        operator=args.operator,
        accept_findings=args.accept_findings,
        reason=args.reason,
    )
    return {
        "status": "approved",
        "record_id": record.record_id,
        "review_id": evidence.review_id,
        "name": record.candidate.name,
        "version": record.candidate.version,
        "digest": record.candidate.digest,
    }


def _deny(args: argparse.Namespace, services: tuple) -> dict[str, object]:
    target = _target(args)
    evidence_store, _, _, _, approvals, _ = services
    evidence = evidence_store.load(target)
    record = approvals.deny(
        evidence,
        operator=args.operator,
        reason=args.reason,
    )
    return {
        "status": "denied",
        "record_id": record.record_id,
        "review_id": evidence.review_id,
        "name": record.candidate.name,
        "digest": record.candidate.digest,
    }


def _activate(args: argparse.Namespace, services: tuple) -> dict[str, object]:
    target = _target(args)
    if (args.replace is None) != (args.expected_digest is None):
        raise ValueError("--replace and --expected-digest must be provided together")
    replacement = (
        PolicyReplacement(
            policy_name=args.replace,
            expected_digest=args.expected_digest,
        )
        if args.replace is not None
        else None
    )
    runtime = services[-1]
    record = runtime.activate(
        target,
        operator=args.operator,
        replacement=replacement,
    )
    return {
        "status": "active",
        "record_id": record.record_id,
        "approval_record_id": record.approval_record_id,
        "name": record.policy_name,
        "digest": record.candidate_digest,
        "replacement": (
            {
                "name": replacement.policy_name,
                "expected_digest": replacement.expected_digest,
            }
            if replacement is not None
            else None
        ),
    }


def _deactivate(args: argparse.Namespace, services: tuple) -> dict[str, object]:
    target = _target(args)
    runtime = services[-1]
    record = runtime.deactivate(
        target,
        operator=args.operator,
        reason=args.reason,
    )
    return {
        "status": "inactive",
        "record_id": record.record_id,
        "name": record.policy_name,
    }


def _rollback(args: argparse.Namespace, services: tuple) -> dict[str, object]:
    target = _target(args)
    runtime = services[-1]
    record = runtime.rollback(
        target,
        operator=args.operator,
        to_approval_id=args.to,
    )
    return {
        "status": "active",
        "action": "rollback",
        "record_id": record.record_id,
        "approval_record_id": record.approval_record_id,
        "name": record.policy_name,
        "digest": record.candidate_digest,
    }


def _status(args: argparse.Namespace, services: tuple) -> dict[str, object]:
    _, activations, _, _, _, runtime = services
    active = [
        {
            "name": item.approval.candidate.name,
            "version": item.approval.candidate.version,
            "digest": item.approval.candidate.digest,
            "approval_record_id": item.approval.record_id,
            "selection_record_id": item.selection.record_id,
            "replacement": (
                {
                    "name": item.selection.replacement.policy_name,
                    "expected_digest": item.selection.replacement.expected_digest,
                }
                if item.selection.replacement is not None
                else None
            ),
        }
        for item in runtime.active()
        if args.target is None or item.approval.candidate.name == args.target
    ]
    approvals = [
        {
            "record_id": record.record_id,
            "name": record.candidate.name,
            "version": record.candidate.version,
            "digest": record.candidate.digest,
            "decision": record.decision.value,
        }
        for record in activations.records()
        if record.candidate.kind == "policy"
        and (args.target is None or record.candidate.name == args.target)
    ]
    return {"active": active, "approvals": approvals}


def _target(args: argparse.Namespace) -> str:
    if not isinstance(args.target, str) or not args.target.strip():
        raise ValueError("Policy command target is required")
    return args.target


def _print(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if "active" in payload:
        active = cast(list[dict[str, Any]], payload["active"])
        if not active:
            print("No active evolved Policies.")
        else:
            for item in active:
                print(
                    f"{item['name']} {item['version']} "
                    f"[active] {item['digest'][:12]}"
                )
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


__all__ = ["policy_init_main", "policy_lifecycle_main"]
