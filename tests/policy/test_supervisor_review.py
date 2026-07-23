from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass, field

import pytest

from evopi.core.tool import ToolCall
from evopi.policy.decisions import PolicyAction, PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel
from evopi.validators import (
    ReplayCase,
    ReplayCaseResult,
    ReplayReport,
    ValidationResult,
    build_policy_review_report,
)


@dataclass(slots=True)
class CandidatePolicy:
    name: str = "candidate"
    version: str = "2"
    description: str = "Candidate Policy"
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 10
    enabled: bool = False
    source: str = "project"
    risk_level: RiskLevel = "low"
    metadata: dict = field(default_factory=dict)
    calls: int = 0

    def run(self, context: PolicyContext) -> PolicyDecision:
        self.calls += 1
        raise AssertionError("Supervisor aggregation must not execute the Policy")


def replay_result(
    *,
    policy_name: str = "candidate",
    status: str = "unchanged",
    recorded_action: PolicyAction | None = "allow",
    candidate_action: PolicyAction = "allow",
) -> ReplayReport:
    recorded = (
        PolicyDecision(action=recorded_action, policy_name=policy_name)
        if recorded_action is not None
        else None
    )
    case = ReplayCase(
        case_id="run:call-1:1",
        run_id="run",
        tool_call=ToolCall(
            id="call-1",
            name="shell_command",
            arguments={"command": "SECRET-RAW-ARGUMENT"},
        ),
        arguments={"command": "SECRET-RAW-ARGUMENT"},
        recorded_decision=recorded,
        source_line=12,
    )
    return ReplayReport(
        policy_name=policy_name,
        results=[
            ReplayCaseResult(
                case=case,
                status=status,  # type: ignore[arg-type]
                decision=PolicyDecision(action=candidate_action),
            )
        ],
    )


def test_all_evidence_passes_without_executing_or_enabling_candidate() -> None:
    policy = CandidatePolicy(risk_level="critical")

    report = build_policy_review_report(
        policy,
        schema_result=ValidationResult(passed=True),
        dry_run_result=ValidationResult(passed=True),
        replay_report=replay_result(),
    )

    assert report.status == "passed"
    assert [check.status for check in report.checks] == [
        "passed",
        "passed",
        "passed",
    ]
    assert report.findings == ()
    assert report.candidate.risk_level == "critical"
    assert policy.calls == 0
    assert policy.enabled is False


def test_warnings_changes_and_new_cases_require_review() -> None:
    warning_report = build_policy_review_report(
        CandidatePolicy(source="generated"),
        schema_result=ValidationResult(
            passed=True,
            warnings=["Generated Policy requires supervisor review before enablement"],
        ),
        dry_run_result=ValidationResult(passed=True),
        replay_report=replay_result(status="changed", candidate_action="block"),
    )
    new_report = build_policy_review_report(
        CandidatePolicy(),
        schema_result=ValidationResult(passed=True),
        dry_run_result=ValidationResult(passed=True),
        replay_report=replay_result(status="new", recorded_action=None),
    )

    assert warning_report.status == "review_required"
    assert {finding.code for finding in warning_report.findings} == {
        "schema_warning",
        "trace_replay_changed",
    }
    assert new_report.status == "review_required"
    assert [finding.code for finding in new_report.findings] == ["trace_replay_new"]


def test_missing_optional_evidence_requires_review() -> None:
    report = build_policy_review_report(
        CandidatePolicy(),
        schema_result=ValidationResult(passed=True),
    )

    assert report.status == "review_required"
    assert [check.status for check in report.checks] == [
        "passed",
        "missing",
        "missing",
    ]
    assert {finding.code for finding in report.findings} == {
        "dry_run_missing",
        "trace_replay_missing",
    }


@pytest.mark.parametrize(
    ("schema", "dry_run", "replay", "expected_code"),
    [
        (
            ValidationResult(passed=False, errors=["bad schema"]),
            ValidationResult(passed=True),
            replay_result(),
            "schema_error",
        ),
        (
            ValidationResult(passed=True),
            ValidationResult(passed=False, errors=["dry-run failed"]),
            replay_result(),
            "dry_run_error",
        ),
        (
            ValidationResult(passed=True),
            ValidationResult(passed=True),
            ReplayReport(policy_name="candidate"),
            "trace_replay_error",
        ),
        (
            ValidationResult(passed=True),
            ValidationResult(passed=True),
            replay_result(policy_name="other"),
            "trace_replay_policy_mismatch",
        ),
    ],
)
def test_failed_evidence_has_precedence(
    schema: ValidationResult,
    dry_run: ValidationResult,
    replay: ReplayReport,
    expected_code: str,
) -> None:
    report = build_policy_review_report(
        CandidatePolicy(),
        schema_result=schema,
        dry_run_result=dry_run,
        replay_report=replay,
    )

    assert report.status == "failed"
    assert expected_code in {finding.code for finding in report.findings}


def test_replay_is_not_applicable_to_other_hooks() -> None:
    policy = CandidatePolicy(hooks=("after_turn",))
    without_replay = build_policy_review_report(
        policy,
        schema_result=ValidationResult(passed=True),
        dry_run_result=ValidationResult(passed=True),
    )
    with_replay = build_policy_review_report(
        policy,
        schema_result=ValidationResult(passed=True),
        dry_run_result=ValidationResult(passed=True),
        replay_report=replay_result(),
    )

    assert without_replay.status == "passed"
    assert without_replay.checks[-1].status == "not_applicable"
    assert with_replay.status == "failed"
    assert with_replay.findings[-1].code == "trace_replay_not_applicable"


def test_case_findings_are_locatable_without_copying_raw_arguments() -> None:
    report = build_policy_review_report(
        CandidatePolicy(),
        schema_result=ValidationResult(passed=True),
        dry_run_result=ValidationResult(passed=True),
        replay_report=replay_result(status="changed", candidate_action="block"),
    )

    finding = report.findings[0]
    assert finding.case_id == "run:call-1:1"
    assert finding.tool_name == "shell_command"
    assert finding.source_line == 12
    assert finding.details == {
        "replay_status": "changed",
        "recorded_action": "allow",
        "candidate_action": "block",
        "rewritten_args_changed": False,
    }
    assert "SECRET-RAW-ARGUMENT" not in json.dumps(report.to_dict())


def test_report_is_versioned_json_ready_and_frozen() -> None:
    report = build_policy_review_report(
        CandidatePolicy(),
        schema_result=ValidationResult(passed=True),
    )
    value = report.to_dict()

    assert value["schema_version"] == 1
    assert len(value["report_id"]) == 32
    assert value["created_at"].endswith("+00:00")
    assert value["candidate"]["hooks"] == ["before_tool_call"]
    json.dumps(value)
    with pytest.raises(FrozenInstanceError):
        report.status = "failed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        report.checks[0].metadata["changed"] = True  # type: ignore[index]
