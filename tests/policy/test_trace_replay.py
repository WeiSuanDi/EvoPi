from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from evopi.coding import CodingHarness
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import Tool, ToolCall, ToolResult
from evopi.harness.confirmation import ConfirmationRequest, ConfirmationResponse
from evopi.policy.builtins import ShellSafetyPolicy
from evopi.policy.decisions import PolicyAction, PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel
from evopi.trace.reader import read_trace
from evopi.trace.writer import JsonlTraceWriter
from evopi.validators import (
    TraceReplayError,
    load_before_tool_replay_cases,
    replay_policy,
)


class ScriptedShellModel:
    name = "scripted-shell"

    def __init__(self) -> None:
        self.calls = 0
        self._messages = iter(
            [
                AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="safe-call",
                            name="shell_command",
                            arguments={"command": "python -m pytest"},
                        ),
                        ToolCall(
                            id="dangerous-call",
                            name="shell_command",
                            arguments={"command": "git reset --hard HEAD"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(content="done", stop_reason="stop"),
            ]
        )

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.calls += 1
        yield ModelComplete(message=next(self._messages))


@dataclass(slots=True)
class FixedPolicy:
    name: str
    action: PolicyAction = "allow"
    rewritten_args: dict | None = None
    version: str = "candidate"
    description: str = "Return one fixed replay decision"
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 100
    enabled: bool = True
    source: str = "test"
    risk_level: RiskLevel = "medium"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(
            action=self.action,
            reason="candidate decision",
            risk_level=self.risk_level,
            rewritten_args=self.rewritten_args,
        )


@dataclass(slots=True)
class ExplodingPolicy(FixedPolicy):
    def run(self, context: PolicyContext) -> PolicyDecision:
        raise RuntimeError("candidate failed")


def _create_coding_trace(tmp_path: Path) -> tuple[Path, ScriptedShellModel, dict[str, int]]:
    trace_path = tmp_path / "coding.jsonl"
    model = ScriptedShellModel()
    calls = {"tool": 0, "confirmation": 0}

    def shell(command: str) -> ToolResult:
        calls["tool"] += 1
        return ToolResult(content=command)

    def approve(request: ConfirmationRequest) -> ConfirmationResponse:
        calls["confirmation"] += 1
        return ConfirmationResponse(request_id=request.id, decision="approve")

    harness = CodingHarness(
        model=model,
        workspace=tmp_path,
        trace_path=trace_path,
        confirmation_handler=approve,
    )
    harness.register_tool(
        Tool(
            name="shell_command",
            description="Test-only shell",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            handler=shell,
        ),
        replace=True,
    )
    asyncio.run(harness.prompt("Run both shell commands"))
    return trace_path, model, calls


def test_real_coding_trace_replays_same_policy_without_runtime_side_effects(
    tmp_path: Path,
) -> None:
    trace_path, model, calls = _create_coding_trace(tmp_path)
    records = list(read_trace(trace_path))
    evaluations = [
        record
        for record in records
        if record["type"] == "policy_evaluation"
        and record["data"]["hook"] == "before_tool_call"
    ]

    assert len(evaluations) == 2
    assert evaluations[0]["data"]["input"]["tool_call"]["name"] == "shell_command"
    assert evaluations[0]["data"]["input"]["arguments"] == {
        "command": "python -m pytest"
    }
    assert evaluations[1]["data"]["final"]["action"] == "block"
    assert model.calls == 2
    assert calls == {"tool": 1, "confirmation": 1}

    cases = load_before_tool_replay_cases(trace_path, policy_name="shell_safety")
    report = asyncio.run(replay_policy(ShellSafetyPolicy(), cases))

    assert [case.recorded_decision.action for case in cases if case.recorded_decision] == [
        "allow",
        "block",
    ]
    assert report.total == 2
    assert report.unchanged_count == 2
    assert report.changed_count == 0
    assert report.passed is True
    assert model.calls == 2
    assert calls == {"tool": 1, "confirmation": 1}


def test_changed_action_and_rewritten_arguments_are_review_only(tmp_path: Path) -> None:
    trace_path, _, _ = _create_coding_trace(tmp_path)
    shell_cases = load_before_tool_replay_cases(trace_path, policy_name="shell_safety")
    confirmation_cases = load_before_tool_replay_cases(
        trace_path, policy_name="tool_confirmation"
    )

    changed_action = asyncio.run(
        replay_policy(
            FixedPolicy(name="shell_safety", action="require_confirmation"),
            shell_cases,
        )
    )
    changed_arguments = asyncio.run(
        replay_policy(
            FixedPolicy(
                name="tool_confirmation",
                action="require_confirmation",
                rewritten_args={"command": "python -m pytest -q"},
            ),
            confirmation_cases,
        )
    )

    assert changed_action.changed_count == 2
    assert changed_arguments.changed_count == 2
    assert changed_action.passed is True
    assert changed_arguments.passed is True


def test_new_policy_has_no_historical_baseline(tmp_path: Path) -> None:
    trace_path, _, _ = _create_coding_trace(tmp_path)
    cases = load_before_tool_replay_cases(trace_path, policy_name="new_candidate")

    report = asyncio.run(replay_policy(FixedPolicy(name="new_candidate"), cases))

    assert report.new_count == 2
    assert report.passed is True


def test_candidate_error_and_empty_cases_fail_the_report(tmp_path: Path) -> None:
    trace_path, _, _ = _create_coding_trace(tmp_path)
    cases = load_before_tool_replay_cases(trace_path, policy_name="shell_safety")

    failed = asyncio.run(replay_policy(ExplodingPolicy(name="shell_safety"), cases))
    empty = asyncio.run(replay_policy(FixedPolicy(name="empty"), []))

    assert failed.error_count == 2
    assert failed.passed is False
    assert all("RuntimeError: candidate failed" in error for error in failed.errors)
    assert empty.total == 0
    assert empty.passed is False
    assert empty.errors


def test_legacy_tool_call_and_policy_decision_trace_is_supported(tmp_path: Path) -> None:
    trace_path = tmp_path / "legacy.jsonl"
    writer = JsonlTraceWriter(trace_path)
    call = ToolCall(
        id="legacy-call",
        name="shell_command",
        arguments={"command": "python -m pytest"},
    )
    writer(CoreEvent(type="tool_call", run_id="legacy-run", data={"tool_call": call}))
    writer(
        CoreEvent(
            type="policy_decision",
            run_id="legacy-run",
            data={
                "hook": "before_tool_call",
                "decision": PolicyDecision(
                    action="allow",
                    reason="legacy baseline",
                    policy_name="shell_safety",
                ),
            },
        )
    )
    writer(
        CoreEvent(
            type="tool_result",
            run_id="legacy-run",
            data={"tool_call": call, "tool_result": ToolResult(content="ok")},
        )
    )

    cases = load_before_tool_replay_cases(trace_path, policy_name="shell_safety")
    report = asyncio.run(replay_policy(ShellSafetyPolicy(), cases))

    assert len(cases) == 1
    assert cases[0].source_line == 1
    assert cases[0].recorded_decision is not None
    assert report.unchanged_count == 1
    assert report.passed is True


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (
            {
                "type": "policy_evaluation",
                "run_id": "run",
                "data": {
                    "hook": "before_tool_call",
                    "input": {
                        "tool_call": {"id": "call", "arguments": {}},
                        "arguments": {},
                    },
                    "decisions": [],
                    "final": {"action": "allow"},
                },
            },
            "tool_call.name",
        ),
        (
            {
                "type": "policy_evaluation",
                "run_id": "run",
                "data": {
                    "hook": "before_tool_call",
                    "input": {
                        "tool_call": {
                            "id": "call",
                            "name": "shell_command",
                            "arguments": [],
                        },
                        "arguments": {},
                    },
                    "decisions": [],
                    "final": {"action": "allow"},
                },
            },
            "tool_call.arguments",
        ),
        (
            {"type": "policy_evaluation", "run_id": "run", "data": []},
            "data must be an object",
        ),
    ],
)
def test_malformed_trace_reports_source_line(
    tmp_path: Path, record: dict, reason: str
) -> None:
    trace_path = tmp_path / "malformed.jsonl"
    trace_path.write_text(
        json.dumps({"type": "agent_start", "run_id": "run", "data": {}})
        + "\n"
        + json.dumps(record)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TraceReplayError) as captured:
        load_before_tool_replay_cases(trace_path, policy_name="shell_safety")

    assert captured.value.line_number == 2
    assert reason in captured.value.reason
