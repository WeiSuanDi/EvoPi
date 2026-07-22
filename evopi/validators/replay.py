"""Offline before-tool Policy replay from JSONL traces."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from evopi.core.context import AgentContext
from evopi.core.tool import ToolCall, ToolResult
from evopi.core.types import JsonObject, Metadata
from evopi.policy.decisions import PolicyAction, PolicyDecision
from evopi.policy.engine import PolicyEngine
from evopi.policy.registry import PolicyRegistry
from evopi.policy.types import HookName, Policy, PolicyContext, RiskLevel

ReplayStatus: TypeAlias = Literal["unchanged", "changed", "new", "error"]

_ACTIONS: frozenset[str] = frozenset(
    {
        "allow",
        "block",
        "rewrite_args",
        "require_confirmation",
        "trigger_validation",
        "terminate",
    }
)
_RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "critical"})


class TraceReplayError(ValueError):
    def __init__(self, line_number: int, reason: str) -> None:
        self.line_number = line_number
        self.reason = reason
        super().__init__(f"Trace line {line_number}: {reason}")


@dataclass(slots=True, kw_only=True)
class ReplayCase:
    case_id: str
    run_id: str | None
    tool_call: ToolCall
    arguments: JsonObject
    recorded_decision: PolicyDecision | None
    source_line: int
    hook: Literal["before_tool_call"] = field(default="before_tool_call", init=False)


@dataclass(slots=True, kw_only=True)
class ReplayCaseResult:
    case: ReplayCase
    status: ReplayStatus
    decision: PolicyDecision | None = None
    error: str | None = None


@dataclass(slots=True, kw_only=True)
class ReplayReport:
    policy_name: str
    results: list[ReplayCaseResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def unchanged_count(self) -> int:
        return self._count("unchanged")

    @property
    def changed_count(self) -> int:
        return self._count("changed")

    @property
    def new_count(self) -> int:
        return self._count("new")

    @property
    def error_count(self) -> int:
        return self._count("error")

    @property
    def passed(self) -> bool:
        return self.total > 0 and self.error_count == 0 and not self.errors

    def _count(self, status: ReplayStatus) -> int:
        return sum(result.status == status for result in self.results)


@dataclass(slots=True)
class _PendingToolCall:
    run_id: str | None
    source_line: int
    tool_call: ToolCall
    decisions: list[PolicyDecision] = field(default_factory=list)


@dataclass(slots=True)
class _EnabledPolicy:
    """Run disabled candidate assets without mutating their registry state."""

    policy: Policy
    name: str = field(init=False)
    version: str = field(init=False)
    description: str = field(init=False)
    hooks: tuple[HookName, ...] = field(init=False)
    priority: int = field(init=False)
    enabled: bool = True
    source: str = field(init=False)
    risk_level: RiskLevel = field(init=False)
    metadata: Metadata = field(init=False)

    def __post_init__(self) -> None:
        self.name = self.policy.name
        self.version = self.policy.version
        self.description = self.policy.description
        self.hooks = self.policy.hooks
        self.priority = self.policy.priority
        self.source = self.policy.source
        self.risk_level = self.policy.risk_level
        self.metadata = dict(self.policy.metadata)

    def run(self, context: PolicyContext) -> Any:
        return self.policy.run(context)


def load_before_tool_replay_cases(
    path: str | Path, *, policy_name: str
) -> list[ReplayCase]:
    """Load tool-scoped cases and the recorded decision for one Policy name."""

    if not policy_name.strip():
        raise ValueError("policy_name must be a non-empty string")

    cases: list[ReplayCase] = []
    pending: dict[str | None, _PendingToolCall] = {}
    ordinal = 0

    def append_pending(item: _PendingToolCall) -> None:
        nonlocal ordinal
        ordinal += 1
        cases.append(
            _make_case(
                run_id=item.run_id,
                source_line=item.source_line,
                tool_call=item.tool_call,
                arguments=item.tool_call.arguments,
                decisions=item.decisions,
                policy_name=policy_name,
                ordinal=ordinal,
            )
        )

    for line_number, record in _read_records(path):
        schema_version = _parse_schema_version(
            record.get("schema_version", 1), line_number
        )
        event_type = record.get("type")
        run_id = _parse_run_id(record.get("run_id"), line_number)

        if event_type == "tool_call":
            previous = pending.pop(run_id, None)
            if previous is not None:
                append_pending(previous)
            data = _require_object(record.get("data"), line_number, "data")
            tool_call = _parse_tool_call(data.get("tool_call"), line_number)
            pending[run_id] = _PendingToolCall(
                run_id=run_id,
                source_line=line_number,
                tool_call=tool_call,
            )
            continue

        if event_type == "tool_execution_start":
            if schema_version != 2:
                raise TraceReplayError(
                    line_number,
                    "tool_execution_start requires Trace schema version 2",
                )
            previous = pending.pop(run_id, None)
            if previous is not None:
                append_pending(previous)
            data = _require_object(record.get("data"), line_number, "data")
            tool_call = _parse_tool_execution_start(data, line_number)
            pending[run_id] = _PendingToolCall(
                run_id=run_id,
                source_line=line_number,
                tool_call=tool_call,
            )
            continue

        if event_type == "policy_decision":
            data = _require_object(record.get("data"), line_number, "data")
            if data.get("hook") != "before_tool_call":
                continue
            item = pending.get(run_id)
            if item is not None:
                item.decisions.append(_parse_decision(data.get("decision"), line_number))
            continue

        if event_type == "policy_evaluation":
            data = _require_object(record.get("data"), line_number, "data")
            if data.get("hook") != "before_tool_call":
                continue
            input_data = _require_object(data.get("input"), line_number, "input")
            tool_call = _parse_tool_call(input_data.get("tool_call"), line_number)
            arguments = _require_object(input_data.get("arguments"), line_number, "arguments")
            decisions = _parse_decisions(data.get("decisions"), line_number)
            _parse_decision(data.get("final"), line_number)

            previous = pending.pop(run_id, None)
            if previous is not None and previous.tool_call.id != tool_call.id:
                raise TraceReplayError(
                    line_number,
                    "policy_evaluation tool_call does not match the active tool_call",
                )
            ordinal += 1
            cases.append(
                _make_case(
                    run_id=run_id,
                    source_line=line_number,
                    tool_call=tool_call,
                    arguments=arguments,
                    decisions=decisions,
                    policy_name=policy_name,
                    ordinal=ordinal,
                )
            )
            continue

        if event_type in {"tool_result", "tool_execution_end"}:
            item = pending.pop(run_id, None)
            if item is not None:
                append_pending(item)

    for item in pending.values():
        append_pending(item)
    return cases


async def replay_policy(policy: Policy, cases: Iterable[ReplayCase]) -> ReplayReport:
    """Run one candidate Policy offline without invoking models or tools."""

    report = ReplayReport(policy_name=policy.name)
    loaded_cases = list(cases)
    if not loaded_cases:
        report.errors.append("Trace replay requires at least one before_tool_call case")
        return report
    if "before_tool_call" not in policy.hooks:
        report.errors.append(
            f"Policy '{policy.name}' is not bound to before_tool_call"
        )
        return report

    engine = PolicyEngine(PolicyRegistry([_EnabledPolicy(policy)]))
    failure_prefix = f"Policy '{policy.name}' failed:"
    for case in loaded_cases:
        evaluation = await engine.evaluate(
            PolicyContext(
                hook="before_tool_call",
                agent_context=AgentContext(),
                tool_call=case.tool_call,
                arguments=dict(case.arguments),
                metadata={
                    "replay_case_id": case.case_id,
                    "trace_source_line": case.source_line,
                },
            )
        )
        decision = evaluation.final
        if decision.reason.startswith(failure_prefix):
            error = decision.reason
            report.results.append(
                ReplayCaseResult(
                    case=case,
                    status="error",
                    decision=decision,
                    error=error,
                )
            )
            report.errors.append(f"{case.case_id}: {error}")
            continue

        recorded = case.recorded_decision
        if recorded is None:
            status: ReplayStatus = "new"
        elif (
            recorded.action != decision.action
            or recorded.rewritten_args != decision.rewritten_args
        ):
            status = "changed"
        else:
            status = "unchanged"
        report.results.append(
            ReplayCaseResult(case=case, status=status, decision=decision)
        )
    return report


def _make_case(
    *,
    run_id: str | None,
    source_line: int,
    tool_call: ToolCall,
    arguments: JsonObject,
    decisions: list[PolicyDecision],
    policy_name: str,
    ordinal: int,
) -> ReplayCase:
    matches = [decision for decision in decisions if decision.policy_name == policy_name]
    if len(matches) > 1:
        raise TraceReplayError(
            source_line,
            f"multiple decisions found for Policy '{policy_name}'",
        )
    case_id = f"{run_id or 'unknown'}:{tool_call.id}:{ordinal}"
    return ReplayCase(
        case_id=case_id,
        run_id=run_id,
        tool_call=tool_call,
        arguments=dict(arguments),
        recorded_decision=matches[0] if matches else None,
        source_line=source_line,
    )


def _read_records(path: str | Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TraceReplayError(line_number, "invalid JSON") from exc
            if not isinstance(record, dict):
                raise TraceReplayError(line_number, "record must be an object")
            yield line_number, record


def _parse_run_id(value: Any, line_number: int) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TraceReplayError(line_number, "run_id must be a string or null")


def _parse_schema_version(value: Any, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TraceReplayError(line_number, "schema_version must be an integer")
    if value not in {1, 2}:
        raise TraceReplayError(
            line_number,
            f"unsupported Trace schema version: {value}",
        )
    return value


def _parse_tool_execution_start(
    data: dict[str, Any], line_number: int
) -> ToolCall:
    return _parse_tool_call(
        {
            "id": data.get("tool_call_id"),
            "name": data.get("tool_name"),
            "arguments": data.get("args"),
        },
        line_number,
    )


def _parse_tool_call(value: Any, line_number: int) -> ToolCall:
    data = _require_object(value, line_number, "tool_call")
    call_id = data.get("id")
    name = data.get("name")
    arguments = data.get("arguments")
    if not isinstance(call_id, str) or not call_id:
        raise TraceReplayError(line_number, "tool_call.id must be a non-empty string")
    if not isinstance(name, str) or not name:
        raise TraceReplayError(line_number, "tool_call.name must be a non-empty string")
    if not isinstance(arguments, dict):
        raise TraceReplayError(line_number, "tool_call.arguments must be an object")
    return ToolCall(id=call_id, name=name, arguments=dict(arguments))


def _parse_decisions(value: Any, line_number: int) -> list[PolicyDecision]:
    if not isinstance(value, list):
        raise TraceReplayError(line_number, "decisions must be an array")
    return [_parse_decision(item, line_number) for item in value]


def _parse_decision(value: Any, line_number: int) -> PolicyDecision:
    data = _require_object(value, line_number, "decision")
    action = data.get("action")
    risk_level = data.get("risk_level", "low")
    reason = data.get("reason", "")
    rewritten_args = data.get("rewritten_args")
    replacement_result = data.get("replacement_result")
    metadata = data.get("metadata", {})
    policy_name = data.get("policy_name")

    if action not in _ACTIONS:
        raise TraceReplayError(line_number, f"unknown Policy action: {action!r}")
    if risk_level not in _RISK_LEVELS:
        raise TraceReplayError(line_number, f"unknown risk level: {risk_level!r}")
    if not isinstance(reason, str):
        raise TraceReplayError(line_number, "decision.reason must be a string")
    if rewritten_args is not None and not isinstance(rewritten_args, dict):
        raise TraceReplayError(line_number, "decision.rewritten_args must be an object or null")
    if not isinstance(metadata, dict):
        raise TraceReplayError(line_number, "decision.metadata must be an object")
    if policy_name is not None and not isinstance(policy_name, str):
        raise TraceReplayError(line_number, "decision.policy_name must be a string or null")

    return PolicyDecision(
        action=cast(PolicyAction, action),
        reason=reason,
        risk_level=cast(RiskLevel, risk_level),
        rewritten_args=dict(rewritten_args) if rewritten_args is not None else None,
        replacement_result=_parse_tool_result(replacement_result, line_number),
        metadata=dict(metadata),
        policy_name=policy_name,
    )


def _parse_tool_result(value: Any, line_number: int) -> ToolResult | None:
    if value is None:
        return None
    data = _require_object(value, line_number, "replacement_result")
    content = data.get("content")
    is_error = data.get("is_error", False)
    terminate = data.get("terminate", False)
    metadata = data.get("metadata", {})
    if not isinstance(content, str):
        raise TraceReplayError(line_number, "replacement_result.content must be a string")
    if not isinstance(is_error, bool) or not isinstance(terminate, bool):
        raise TraceReplayError(
            line_number,
            "replacement_result flags must be booleans",
        )
    if not isinstance(metadata, dict):
        raise TraceReplayError(line_number, "replacement_result.metadata must be an object")
    return ToolResult(
        content=content,
        is_error=is_error,
        terminate=terminate,
        metadata=dict(metadata),
    )


def _require_object(value: Any, line_number: int, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TraceReplayError(line_number, f"{name} must be an object")
    return value


__all__ = [
    "ReplayCase",
    "ReplayCaseResult",
    "ReplayReport",
    "ReplayStatus",
    "TraceReplayError",
    "load_before_tool_replay_cases",
    "replay_policy",
]
