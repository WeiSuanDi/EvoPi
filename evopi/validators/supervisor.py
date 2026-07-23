"""Deterministic aggregation of Policy validation evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from evopi.policy.types import Policy
from evopi.validators.base import ValidationResult
from evopi.validators.replay import ReplayReport

SupervisorStatus: TypeAlias = Literal["passed", "review_required", "failed"]
SupervisorCheckStatus: TypeAlias = Literal[
    "passed",
    "failed",
    "missing",
    "not_applicable",
]
SupervisorFindingSeverity: TypeAlias = Literal["warning", "error"]
SupervisorCheckName: TypeAlias = Literal["schema", "dry_run", "trace_replay"]


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyCandidateSnapshot:
    name: str
    version: str
    hooks: tuple[str, ...]
    source: str
    risk_level: str
    enabled: bool


@dataclass(slots=True, frozen=True, kw_only=True)
class SupervisorCheckResult:
    name: SupervisorCheckName
    status: SupervisorCheckStatus
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(slots=True, frozen=True, kw_only=True)
class SupervisorFinding:
    code: str
    severity: SupervisorFindingSeverity
    message: str
    check: SupervisorCheckName
    case_id: str | None = None
    tool_name: str | None = None
    source_line: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(slots=True, frozen=True, kw_only=True)
class SupervisorReport:
    candidate: PolicyCandidateSnapshot
    status: SupervisorStatus
    checks: tuple[SupervisorCheckResult, ...]
    findings: tuple[SupervisorFinding, ...]
    schema_version: int = 1
    report_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "created_at": self.created_at.isoformat(),
            "candidate": {
                "name": self.candidate.name,
                "version": self.candidate.version,
                "hooks": list(self.candidate.hooks),
                "source": self.candidate.source,
                "risk_level": self.candidate.risk_level,
                "enabled": self.candidate.enabled,
            },
            "status": self.status,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "errors": list(check.errors),
                    "warnings": list(check.warnings),
                    "metadata": _json_ready(check.metadata),
                }
                for check in self.checks
            ],
            "findings": [
                {
                    "code": finding.code,
                    "severity": finding.severity,
                    "message": finding.message,
                    "check": finding.check,
                    "case_id": finding.case_id,
                    "tool_name": finding.tool_name,
                    "source_line": finding.source_line,
                    "details": _json_ready(finding.details),
                }
                for finding in self.findings
            ],
        }


def build_policy_review_report(
    policy: Policy,
    *,
    schema_result: ValidationResult,
    dry_run_result: ValidationResult | None = None,
    replay_report: ReplayReport | None = None,
) -> SupervisorReport:
    """Combine existing evidence without executing validators or mutating the Policy."""

    candidate = _snapshot_policy(policy)
    checks: list[SupervisorCheckResult] = []
    findings: list[SupervisorFinding] = []

    checks.append(
        _validation_check(
            name="schema",
            result=schema_result,
            error_code="schema_error",
            warning_code="schema_warning",
            findings=findings,
        )
    )

    if dry_run_result is None:
        checks.append(SupervisorCheckResult(name="dry_run", status="missing"))
        findings.append(
            SupervisorFinding(
                code="dry_run_missing",
                severity="warning",
                message="Dry-run evidence was not provided",
                check="dry_run",
            )
        )
    else:
        checks.append(
            _validation_check(
                name="dry_run",
                result=dry_run_result,
                error_code="dry_run_error",
                warning_code="dry_run_warning",
                findings=findings,
            )
        )

    replay_applicable = "before_tool_call" in candidate.hooks
    if not replay_applicable:
        if replay_report is None:
            checks.append(
                SupervisorCheckResult(
                    name="trace_replay",
                    status="not_applicable",
                )
            )
        else:
            message = "Trace replay evidence is not applicable to this Policy"
            checks.append(
                SupervisorCheckResult(
                    name="trace_replay",
                    status="failed",
                    errors=(message,),
                )
            )
            findings.append(
                SupervisorFinding(
                    code="trace_replay_not_applicable",
                    severity="error",
                    message=message,
                    check="trace_replay",
                )
            )
    elif replay_report is None:
        checks.append(SupervisorCheckResult(name="trace_replay", status="missing"))
        findings.append(
            SupervisorFinding(
                code="trace_replay_missing",
                severity="warning",
                message="Trace replay evidence was not provided",
                check="trace_replay",
            )
        )
    else:
        checks.append(
            _replay_check(
                candidate=candidate,
                report=replay_report,
                findings=findings,
            )
        )

    if any(check.status == "failed" for check in checks):
        status: SupervisorStatus = "failed"
    elif any(
        check.status == "missing" or check.warnings for check in checks
    ) or any(finding.severity == "warning" for finding in findings):
        status = "review_required"
    else:
        status = "passed"

    return SupervisorReport(
        candidate=candidate,
        status=status,
        checks=tuple(checks),
        findings=tuple(findings),
    )


def _snapshot_policy(policy: Policy) -> PolicyCandidateSnapshot:
    raw_hooks = getattr(policy, "hooks", ())
    hooks = (
        tuple(str(hook) for hook in raw_hooks)
        if isinstance(raw_hooks, (list, tuple))
        else ()
    )
    return PolicyCandidateSnapshot(
        name=_safe_text(getattr(policy, "name", None), "<invalid>"),
        version=_safe_text(getattr(policy, "version", None), "<invalid>"),
        hooks=hooks,
        source=_safe_text(getattr(policy, "source", None), "<invalid>"),
        risk_level=_safe_text(getattr(policy, "risk_level", None), "<invalid>"),
        enabled=bool(getattr(policy, "enabled", False)),
    )


def _validation_check(
    *,
    name: Literal["schema", "dry_run"],
    result: ValidationResult,
    error_code: str,
    warning_code: str,
    findings: list[SupervisorFinding],
) -> SupervisorCheckResult:
    errors = tuple(result.errors)
    if not result.passed and not errors:
        errors = (f"{name} validation failed without an error message",)
    warnings = tuple(result.warnings)
    for message in errors:
        findings.append(
            SupervisorFinding(
                code=error_code,
                severity="error",
                message=message,
                check=name,
            )
        )
    for message in warnings:
        findings.append(
            SupervisorFinding(
                code=warning_code,
                severity="warning",
                message=message,
                check=name,
            )
        )
    return SupervisorCheckResult(
        name=name,
        status="passed" if result.passed else "failed",
        errors=errors,
        warnings=warnings,
    )


def _replay_check(
    *,
    candidate: PolicyCandidateSnapshot,
    report: ReplayReport,
    findings: list[SupervisorFinding],
) -> SupervisorCheckResult:
    metadata = {
        "policy_name": report.policy_name,
        "total": report.total,
        "unchanged": report.unchanged_count,
        "changed": report.changed_count,
        "new": report.new_count,
        "error": report.error_count,
    }
    if report.policy_name != candidate.name:
        message = (
            f"Replay Policy name '{report.policy_name}' does not match "
            f"candidate '{candidate.name}'"
        )
        findings.append(
            SupervisorFinding(
                code="trace_replay_policy_mismatch",
                severity="error",
                message=message,
                check="trace_replay",
            )
        )
        return SupervisorCheckResult(
            name="trace_replay",
            status="failed",
            errors=(message,),
            metadata=metadata,
        )

    errors = list(report.errors)
    errors.extend(
        result.error
        for result in report.results
        if result.status == "error" and result.error is not None
    )
    errors = list(dict.fromkeys(errors))
    if not report.passed and not errors:
        errors.append("Trace replay requires at least one successful case")
    for message in errors:
        findings.append(
            SupervisorFinding(
                code="trace_replay_error",
                severity="error",
                message=message,
                check="trace_replay",
            )
        )

    for result in report.results:
        if result.status not in {"changed", "new"}:
            continue
        recorded = result.case.recorded_decision
        candidate_decision = result.decision
        details = {
            "replay_status": result.status,
            "recorded_action": recorded.action if recorded is not None else None,
            "candidate_action": (
                candidate_decision.action if candidate_decision is not None else None
            ),
            "rewritten_args_changed": (
                recorded is None
                or candidate_decision is None
                or recorded.rewritten_args != candidate_decision.rewritten_args
            ),
        }
        findings.append(
            SupervisorFinding(
                code=f"trace_replay_{result.status}",
                severity="warning",
                message=(
                    f"Replay case {result.case.case_id} is {result.status}"
                ),
                check="trace_replay",
                case_id=result.case.case_id,
                tool_name=result.case.tool_call.name,
                source_line=result.case.source_line,
                details=details,
            )
        )

    return SupervisorCheckResult(
        name="trace_replay",
        status="passed" if report.passed else "failed",
        errors=tuple(errors),
        metadata=metadata,
    )


def _safe_text(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _json_ready(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return repr(value)


__all__ = [
    "PolicyCandidateSnapshot",
    "SupervisorCheckResult",
    "SupervisorCheckStatus",
    "SupervisorFinding",
    "SupervisorFindingSeverity",
    "SupervisorReport",
    "SupervisorStatus",
    "build_policy_review_report",
]
