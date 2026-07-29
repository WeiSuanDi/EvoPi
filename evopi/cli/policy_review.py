"""CLI orchestration for deterministic offline Policy review."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, cast

from evopi.policy.types import Policy, PolicyContext
from evopi.evolution import (
    PolicyEvidenceStore,
    PolicyReviewEvidence,
    PolicyReviewService,
    resolve_evolution_home,
)
from evopi.validators import (
    PolicySchemaValidator,
    ReplayReport,
    SupervisorReport,
    TraceReplayError,
    ValidationResult,
    build_policy_review_report,
    dry_run_policy,
    load_before_tool_replay_cases,
    replay_policy,
)

_EXIT_CODES = {"passed": 0, "review_required": 2, "failed": 1}


def build_policy_review_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evopi policy review",
        description="Review one Policy candidate using offline validation evidence",
    )
    parser.add_argument(
        "policy",
        metavar="MODULE:ATTRIBUTE",
        help="Policy instance, zero-argument class, or zero-argument factory",
    )
    parser.add_argument(
        "--dry-run-cases",
        metavar="MODULE:ATTRIBUTE",
        help="PolicyContext iterable or zero-argument factory returning one",
    )
    parser.add_argument(
        "--trace",
        type=Path,
        help="JSONL Trace used for before_tool_call replay",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Write the stable JSON report to stdout",
    )
    parser.add_argument(
        "--review-timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Formal candidate worker timeout (default: 30)",
    )
    return parser


async def review_policy_from_args(args: argparse.Namespace) -> SupervisorReport:
    policy = load_policy_reference(args.policy)
    schema_result = PolicySchemaValidator().validate(policy)
    dry_run_result: ValidationResult | None = None
    replay_report: ReplayReport | None = None

    if schema_result.passed:
        dry_run_spec = getattr(args, "dry_run_cases", None)
        if dry_run_spec is not None:
            try:
                cases = load_dry_run_cases_reference(dry_run_spec)
            except Exception as exc:
                dry_run_result = ValidationResult(
                    passed=False,
                    errors=[
                        f"Dry-run cases could not be loaded: "
                        f"{type(exc).__name__}: {exc}"
                    ],
                )
            else:
                dry_run_result = await dry_run_policy(policy, cases)

        trace_path = getattr(args, "trace", None)
        if trace_path is not None:
            if "before_tool_call" not in policy.hooks:
                raise ValueError(
                    "--trace is only valid for a Policy bound to before_tool_call"
                )
            replay_report = await _run_trace_replay(policy, trace_path)

    return build_policy_review_report(
        policy,
        schema_result=schema_result,
        dry_run_result=dry_run_result,
        replay_report=replay_report,
    )


def policy_review_main(argv: Sequence[str]) -> int:
    args = build_policy_review_parser().parse_args(list(argv))
    try:
        candidate_path = Path(args.policy).expanduser()
        if candidate_path.is_dir():
            if args.dry_run_cases is not None:
                raise ValueError(
                    "Formal candidate Dry Run must be declared in its manifest"
                )
            if args.review_timeout <= 0:
                raise ValueError("--review-timeout must be greater than zero")
            store = PolicyEvidenceStore(
                resolve_evolution_home() / "reviews" / "policies"
            )
            evidence = PolicyReviewService(
                store,
                timeout=args.review_timeout,
            ).review(candidate_path, trace_path=args.trace)
            _print_formal_evidence(evidence, json_output=args.json_output)
            return _EXIT_CODES[evidence.status]
        report = asyncio.run(review_policy_from_args(args))
    except Exception as exc:
        print(f"EvoPi policy review error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nEvoPi policy review aborted.", file=sys.stderr)
        return 130

    if args.json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, separators=(",", ":")))
    else:
        print(render_policy_review(report))
    return _EXIT_CODES[report.status]


def _print_formal_evidence(
    evidence: PolicyReviewEvidence,
    *,
    json_output: bool,
) -> None:
    payload = evidence.to_dict()
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    print(render_policy_review(evidence.supervisor_report))
    print(f"Review evidence: {evidence.review_id}")
    print(f"Candidate digest: {evidence.candidate.digest}")
    print("The evidence is immutable and has not approved or activated the Policy.")


def load_policy_reference(spec: str) -> Policy:
    value = _load_reference(spec)
    if isinstance(value, type):
        value = value()
    elif not callable(getattr(value, "run", None)) and callable(value):
        value = value()
    return cast(Policy, value)


def load_dry_run_cases_reference(spec: str) -> list[PolicyContext]:
    value = _load_reference(spec)
    if callable(value) and not isinstance(value, Iterable):
        value = value()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError("Dry-run cases must be an iterable of PolicyContext objects")
    cases = list(value)
    invalid = [
        index
        for index, case in enumerate(cases, start=1)
        if not isinstance(case, PolicyContext)
    ]
    if invalid:
        joined = ", ".join(str(index) for index in invalid)
        raise TypeError(f"Dry-run cases must contain PolicyContext objects: {joined}")
    return cases


def render_policy_review(report: SupervisorReport) -> str:
    lines = [
        f"Supervisor review: {report.status}",
        (
            f"Policy: {report.candidate.name} "
            f"(version={report.candidate.version}, source={report.candidate.source}, "
            f"risk={report.candidate.risk_level})"
        ),
        f"Report: {report.report_id}",
        "Checks:",
    ]
    for check in report.checks:
        lines.append(f"  [{check.status.upper()}] {check.name}")
        lines.extend(f"    error: {message}" for message in check.errors)
        lines.extend(f"    warning: {message}" for message in check.warnings)
    if report.findings:
        lines.append("Findings:")
        for finding in report.findings:
            location = (
                f" case={finding.case_id}"
                if finding.case_id is not None
                else ""
            )
            lines.append(
                f"  [{finding.severity.upper()}] {finding.code}:{location} "
                f"{finding.message}"
            )
    else:
        lines.append("Findings: none")
    lines.append("This report is technical evidence, not activation approval.")
    return "\n".join(lines)


async def _run_trace_replay(policy: Policy, path: Path) -> ReplayReport:
    try:
        cases = load_before_tool_replay_cases(path, policy_name=policy.name)
    except (OSError, TraceReplayError, ValueError) as exc:
        return ReplayReport(
            policy_name=policy.name,
            errors=[f"Trace replay could not be loaded: {type(exc).__name__}: {exc}"],
        )
    return await replay_policy(policy, cases)


def _load_reference(spec: str) -> Any:
    module_name, separator, attribute_path = spec.partition(":")
    if not separator or not module_name or not attribute_path:
        raise ValueError("Reference must use MODULE:ATTRIBUTE syntax")
    module = importlib.import_module(module_name)
    value: Any = module
    for part in attribute_path.split("."):
        if not part:
            raise ValueError("Reference attribute cannot contain empty segments")
        value = getattr(value, part)
    return value


__all__ = [
    "build_policy_review_parser",
    "load_dry_run_cases_reference",
    "load_policy_reference",
    "policy_review_main",
    "render_policy_review",
    "review_policy_from_args",
]
