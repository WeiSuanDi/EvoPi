from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from evopi.core.context import AgentContext
from evopi.core.agent_loop import TurnLimitError
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


@dataclass
class TerminateAfterTurnPolicy:
    name: str = "terminate_after_turn"
    version: str = "1"
    description: str = "Stop after the current turn"
    hooks: tuple = ("after_turn",)
    priority: int = 10
    enabled: bool = True
    source: str = "test"
    risk_level: str = "low"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action="terminate", reason="Turn is complete")


@dataclass
class TerminateAfterToolPolicy:
    name: str = "terminate_after_tool"
    version: str = "1"
    description: str = "Treat the tool result as the final artifact"
    hooks: tuple = ("after_tool_call",)
    priority: int = 10
    enabled: bool = True
    source: str = "test"
    risk_level: str = "low"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action="terminate", reason="Tool result is final")


@dataclass
class RecordAbortPolicy:
    values: list[bool]
    name: str = "record_abort"
    version: str = "1"
    description: str = "Record the aborted state"
    hooks: tuple = ("after_turn",)
    priority: int = 1
    enabled: bool = True
    source: str = "test"
    risk_level: str = "low"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        self.values.append(context.aborted)
        return PolicyDecision(action="allow")


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
    assert harness.state.end_reason == "completed"
    records = list(read_trace(trace_path))
    assert "policy_decision" in {record["type"] for record in records}
    final_assistant = [
        record
        for record in records
        if record["type"] == "message_end"
        and record["data"]["message"]["role"] == "assistant"
    ][-1]
    assert final_assistant["data"]["message"]["content"] == "The command was blocked."
    assert records[-1]["type"] == "agent_end"
    assert records[-1]["data"]["reason"] == "completed"
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


def test_abort_signal_reaches_context_provider_and_policy() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        provider_observed: list[bool] = []
        policy_observed: list[bool] = []
        model = ScriptedModel([AssistantMessage(content="unreachable", stop_reason="stop")])
        harness = BaseHarness(model=model)

        async def provider(context: AgentContext, *, signal=None) -> None:
            entered.set()
            await release.wait()
            provider_observed.append(bool(signal and signal.aborted))

        harness.add_context_provider(provider)
        harness.register_policy(RecordAbortPolicy(values=policy_observed))
        task = asyncio.create_task(harness.prompt("stop while preparing context"))
        await entered.wait()

        harness.abort()
        assert harness.state.status is LifecycleState.ABORTING
        release.set()
        answer = await task

        assert answer.stop_reason == "aborted"
        assert provider_observed == [True]
        assert policy_observed == [True]
        assert model.contexts == []
        assert harness.state.status is LifecycleState.ABORTED

    asyncio.run(scenario())


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


def test_after_turn_terminate_policy_stops_through_dedicated_callback(tmp_path) -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="echo-1", name="echo", arguments={"value": "ok"})],
                stop_reason="tool_use",
            )
        ]
    )
    trace_path = tmp_path / "after-turn-terminate.jsonl"
    harness = BaseHarness(model=model, trace_path=trace_path)
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
    harness.register_policy(TerminateAfterTurnPolicy())

    asyncio.run(harness.prompt("stop after this turn"))

    assert harness.state.status is LifecycleState.COMPLETED
    assert harness.state.end_reason == "terminated"
    result = next(
        message for message in harness.messages if isinstance(message, ToolResultMessage)
    )
    assert result.terminate is False
    agent_end = next(record for record in read_trace(trace_path) if record["type"] == "agent_end")
    assert agent_end["data"]["reason"] == "terminated"


def test_after_tool_policy_changes_the_final_batch_termination_hint() -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="The tool result is the final artifact.",
                tool_calls=[ToolCall(id="echo-1", name="echo", arguments={"value": "ok"})],
                stop_reason="tool_use",
            )
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
    harness.register_policy(TerminateAfterToolPolicy())

    asyncio.run(harness.prompt("produce the artifact"))

    result = next(
        message for message in harness.messages if isinstance(message, ToolResultMessage)
    )
    assert result.terminate is True
    assert harness.state.status is LifecycleState.COMPLETED
    assert harness.state.end_reason == "terminated"


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
    assert harness.state.end_reason == "error"
    records = list(read_trace(trace_path))
    assert any(record["type"] == "error" for record in records)
    assert any(
        record["type"] == "policy_decision" and record["data"]["hook"] == "on_error"
        for record in records
    )
    assert any(
        record["type"] == "policy_evaluation"
        and record["data"]["hook"] == "on_error"
        and record["data"]["input"]["error"] == "RuntimeError: provider failed"
        for record in records
    )
    assert records[-1]["type"] == "agent_end"
    assert records[-1]["data"]["reason"] == "error"


def test_turn_limit_maps_to_failed_harness_state() -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="echo-1", name="echo", arguments={"value": "ok"})],
                stop_reason="tool_use",
            )
        ]
    )
    harness = BaseHarness(model=model, max_turns=1)
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

    with pytest.raises(TurnLimitError):
        asyncio.run(harness.prompt("loop"))

    assert harness.state.status is LifecycleState.FAILED
    assert harness.state.end_reason == "turn_limit"
