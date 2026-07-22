from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage, SystemMessage, ToolResultMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import Tool, ToolCall
from evopi.harness.base import BaseHarness
from evopi.harness.runtime_state import LifecycleState
from evopi.policy.builtins import ShellSafetyPolicy
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import PolicyContext
from evopi.trace.reader import read_trace


class ScriptedModel:
    name = "scripted"

    def __init__(self, messages: list[AssistantMessage]) -> None:
        self._messages = iter(messages)
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context)
        yield ModelComplete(message=next(self._messages))


@dataclass
class RecordingPolicy:
    calls: list[str]
    name: str = "recording"
    version: str = "1"
    description: str = "Record every MVP hook"
    hooks: tuple = (
        "before_model_call",
        "after_model_call",
        "before_tool_call",
        "after_tool_call",
        "after_turn",
        "on_error",
    )
    priority: int = 1
    enabled: bool = True
    source: str = "test"
    risk_level: str = "low"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        self.calls.append(context.hook)
        return PolicyDecision(action="allow", reason=f"observed {context.hook}")


class FailingModel:
    name = "failing"

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        if False:  # pragma: no cover - makes this function an async generator.
            yield ModelComplete(message=AssistantMessage(content="unreachable"))
        raise RuntimeError("provider failed")


def test_harness_dispatches_policy_and_writes_trace(tmp_path) -> None:
    executed = False

    def dangerous(command: str) -> str:
        nonlocal executed
        executed = True
        return command

    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="shell_command",
                        arguments={"command": "git reset --hard HEAD"},
                    )
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="The command was blocked.", stop_reason="stop"),
        ]
    )
    trace_path = tmp_path / "trace.jsonl"
    harness = BaseHarness(model=model, trace_path=trace_path)
    harness.register_tool(
        Tool(
            name="shell_command",
            description="Run command",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            handler=dangerous,
        )
    )
    harness.register_policy(ShellSafetyPolicy())

    answer = asyncio.run(harness.prompt("Run a dangerous command"))

    assert answer.content == "The command was blocked."
    assert executed is False
    result = next(message for message in harness.messages if isinstance(message, ToolResultMessage))
    assert result.is_error is True
    assert result.metadata["blocked"] is True
    assert harness.state.status is LifecycleState.COMPLETED
    records = list(read_trace(trace_path))
    assert "policy_decision" in {record["type"] for record in records}
    assert "final_message" in {record["type"] for record in records}
    assert len({record["run_id"] for record in records}) == 1
    assert records[0]["run_id"] is not None


def test_context_provider_changes_snapshot_not_transcript() -> None:
    model = ScriptedModel([AssistantMessage(content="ok", stop_reason="stop")])
    harness = BaseHarness(model=model)

    def add_context(context: AgentContext) -> AgentContext:
        context.messages.append(SystemMessage(content="temporary context"))
        return context

    harness.add_context_provider(add_context)
    asyncio.run(harness.prompt("hello"))

    assert any(
        isinstance(message, SystemMessage) and message.content == "temporary context"
        for message in model.contexts[0].messages
    )
    assert not any(
        isinstance(message, SystemMessage) and message.content == "temporary context"
        for message in harness.messages
    )


def test_all_non_error_harness_hooks_are_dispatched() -> None:
    calls: list[str] = []
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="echo-1", name="echo", arguments={"value": "ok"})],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="done", stop_reason="stop"),
        ]
    )
    harness = BaseHarness(model=model)
    harness.register_tool(
        Tool(
            name="echo",
            description="Echo text",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            handler=lambda value: value,
        )
    )
    harness.register_policy(RecordingPolicy(calls=calls))

    asyncio.run(harness.prompt("exercise hooks"))

    assert calls.count("before_model_call") == 2
    assert calls.count("after_model_call") == 2
    assert calls.count("before_tool_call") == 1
    assert calls.count("after_tool_call") == 1
    assert calls.count("after_turn") == 2
    assert "on_error" not in calls


def test_error_hook_trace_and_failed_lifecycle(tmp_path) -> None:
    calls: list[str] = []
    trace_path = tmp_path / "error.jsonl"
    harness = BaseHarness(model=FailingModel(), trace_path=trace_path)
    harness.register_policy(RecordingPolicy(calls=calls))

    try:
        asyncio.run(harness.prompt("fail"))
    except RuntimeError as exc:
        assert str(exc) == "provider failed"
    else:  # pragma: no cover
        raise AssertionError("failing model should fail the run")

    assert calls == ["before_model_call", "on_error"]
    assert harness.state.status is LifecycleState.FAILED
    records = list(read_trace(trace_path))
    assert any(record["type"] == "error" for record in records)
    assert any(
        record["type"] == "policy_decision" and record["data"]["hook"] == "on_error"
        for record in records
    )
