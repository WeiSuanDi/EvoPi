from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path

import pytest

from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import ToolCall
from evopi.core.tool import Tool
from evopi.harness.base import BaseHarness
from evopi.harness.confirmation import (
    ConfirmationHandler,
    ConfirmationRequest,
    ConfirmationResponse,
)
from evopi.harness.lifecycle import Lifecycle
from evopi.harness.runtime_state import LifecycleState
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel
from evopi.trace.reader import read_trace


class ScriptedModel:
    name = "scripted"

    def __init__(self) -> None:
        self._messages = iter(
            [
                AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="echo",
                            arguments={"value": "original"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(content="done", stop_reason="stop"),
            ]
        )

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        yield ModelComplete(message=next(self._messages))


@dataclass(slots=True)
class RequireConfirmationPolicy:
    name: str = "confirm_echo"
    version: str = "1"
    description: str = "Require confirmation before echo"
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 10
    enabled: bool = True
    source: str = "test"
    risk_level: RiskLevel = "medium"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        if context.tool_call is None or context.tool_call.name != "echo":
            return PolicyDecision()
        return PolicyDecision(
            action="require_confirmation",
            reason="Echo requires human approval",
            risk_level=self.risk_level,
            rewritten_args={"value": "rewritten"},
        )


def _build_harness(
    *,
    handler: ConfirmationHandler | None,
    executed: list[str],
    trace_path: Path | None = None,
) -> BaseHarness:
    harness = BaseHarness(
        model=ScriptedModel(),
        confirmation_handler=handler,
        trace_path=trace_path,
    )
    harness.register_tool(
        Tool(
            name="echo",
            description="Echo a value",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            handler=lambda value: executed.append(value) or value,
        )
    )
    harness.register_policy(RequireConfirmationPolicy())
    return harness


def test_confirmation_request_captures_policy_and_tool_context() -> None:
    call = ToolCall(
        id="call-1",
        name="shell_command",
        arguments={"command": "python -m pytest"},
    )

    request = ConfirmationRequest(
        hook="before_tool_call",
        reason="Shell execution requires approval",
        risk_level="high",
        policy_names=("shell_confirmation",),
        tool_call=call,
        arguments={"command": "python -m pytest -q"},
        metadata={"workspace": "demo"},
    )

    assert request.id
    assert request.created_at.tzinfo is UTC
    assert request.hook == "before_tool_call"
    assert request.risk_level == "high"
    assert request.policy_names == ("shell_confirmation",)
    assert request.tool_call is call
    assert request.arguments == {"command": "python -m pytest -q"}
    assert request.metadata == {"workspace": "demo"}


def test_confirmation_request_defaults_are_isolated() -> None:
    first = ConfirmationRequest(hook="before_model_call", reason="first")
    second = ConfirmationRequest(hook="before_model_call", reason="second")

    first.metadata["source"] = "test"

    assert first.id != second.id
    assert second.metadata == {}


def test_confirmation_response_exposes_explicit_decision() -> None:
    approved = ConfirmationResponse(request_id="request-1", decision="approve")
    denied = ConfirmationResponse(
        request_id="request-2",
        decision="deny",
        reason="Operation rejected by the user",
    )

    assert approved.approved is True
    assert denied.approved is False
    assert denied.reason == "Operation rejected by the user"


def test_approved_confirmation_resumes_and_executes_rewritten_tool_args(tmp_path) -> None:
    executed: list[str] = []
    observed_states: list[LifecycleState] = []
    captured_requests: list[ConfirmationRequest] = []
    harness: BaseHarness

    async def approve(request: ConfirmationRequest) -> ConfirmationResponse:
        observed_states.append(harness.state.status)
        captured_requests.append(request)
        return ConfirmationResponse(request_id=request.id, decision="approve")

    trace_path = tmp_path / "confirmation.jsonl"
    harness = _build_harness(handler=approve, executed=executed, trace_path=trace_path)

    answer = asyncio.run(harness.prompt("echo"))

    assert answer.content == "done"
    assert executed == ["rewritten"]
    assert observed_states == [LifecycleState.WAITING_FOR_CONFIRMATION]
    assert harness.state.status is LifecycleState.COMPLETED
    assert captured_requests[0].policy_names == ("confirm_echo",)
    assert captured_requests[0].arguments == {"value": "rewritten"}

    records = list(read_trace(trace_path))
    request_record = next(item for item in records if item["type"] == "confirmation_request")
    response_record = next(item for item in records if item["type"] == "confirmation_response")
    assert response_record["data"]["response"]["request_id"] == request_record["data"][
        "request"
    ]["id"]
    assert request_record["run_id"] == response_record["run_id"]


def test_denied_confirmation_blocks_tool_but_run_can_finish() -> None:
    executed: list[str] = []

    def deny(request: ConfirmationRequest) -> ConfirmationResponse:
        return ConfirmationResponse(
            request_id=request.id,
            decision="deny",
            reason="User rejected the operation",
        )

    harness = _build_harness(handler=deny, executed=executed)

    asyncio.run(harness.prompt("echo"))

    assert executed == []
    result = next(
        message for message in harness.messages if isinstance(message, ToolResultMessage)
    )
    assert result.is_error is True
    assert result.content == "User rejected the operation"
    assert harness.state.status is LifecycleState.COMPLETED


def test_missing_confirmation_handler_fails_closed(tmp_path) -> None:
    executed: list[str] = []
    trace_path = tmp_path / "missing-handler.jsonl"
    harness = _build_harness(handler=None, executed=executed, trace_path=trace_path)

    asyncio.run(harness.prompt("echo"))

    assert executed == []
    result = next(
        message for message in harness.messages if isinstance(message, ToolResultMessage)
    )
    assert "no handler is configured" in result.content
    response_record = next(
        item for item in read_trace(trace_path) if item["type"] == "confirmation_response"
    )
    assert response_record["data"]["response"]["decision"] == "deny"
    assert response_record["data"]["response"]["metadata"]["automatic"] is True


def test_confirmation_handler_error_fails_closed_and_restores_lifecycle() -> None:
    executed: list[str] = []

    def fail(request: ConfirmationRequest) -> ConfirmationResponse:
        raise RuntimeError("approval service unavailable")

    harness = _build_harness(handler=fail, executed=executed)

    asyncio.run(harness.prompt("echo"))

    assert executed == []
    result = next(
        message for message in harness.messages if isinstance(message, ToolResultMessage)
    )
    assert "Confirmation handler failed: RuntimeError" in result.content
    assert harness.state.status is LifecycleState.COMPLETED


def test_waiting_for_confirmation_is_still_an_active_run() -> None:
    lifecycle = Lifecycle()
    lifecycle.start()
    lifecycle.wait_for_confirmation()

    with pytest.raises(RuntimeError, match="already running"):
        lifecycle.start()

    assert lifecycle.state.status is LifecycleState.WAITING_FOR_CONFIRMATION
