from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from evopi.coding import CodingHarness, FinalTurnToolPolicy
from evopi.core.agent_loop import TurnLimitError
from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage, SystemMessage, ToolResultMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import ToolCall
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import PolicyContext
from evopi.trace import read_trace


class ScriptedModel:
    name = "scripted"
    context_window = 0

    def __init__(self, messages: list[AssistantMessage]) -> None:
        self._messages = iter(messages)
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context)
        yield ModelComplete(message=next(self._messages))


def test_final_turn_policy_is_part_of_the_coding_public_api() -> None:
    assert FinalTurnToolPolicy.__name__ == "FinalTurnToolPolicy"


@dataclass(slots=True)
class RecordFinalContextPolicy:
    tool_names: list[tuple[str, ...]]
    name: str = "record_final_context"
    version: str = "1.0.0"
    description: str = "Record the final prepared model context."
    hooks: tuple = ("before_model_call",)
    priority: int = 1
    enabled: bool = True
    source: str = "test"
    risk_level: str = "low"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        self.tool_names.append(tuple(tool.name for tool in context.agent_context.tools))
        return PolicyDecision(action="allow")


def test_coding_harness_warns_then_finalizes_with_no_tools_before_policy(
    tmp_path,
) -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(id="list-1", name="list_dir", arguments={"path": "."})
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="verified result", stop_reason="stop"),
        ]
    )
    trace_path = tmp_path / "trace.jsonl"
    harness = CodingHarness(
        model=model,
        workspace=tmp_path,
        memory_path=None,
        max_turns=2,
        trace_path=trace_path,
    )
    policy_views: list[tuple[str, ...]] = []
    harness.register_policy(RecordFinalContextPolicy(tool_names=policy_views))

    answer = asyncio.run(harness.prompt("finish within the budget"))

    assert answer.content == "verified result"
    assert len(model.contexts[0].tools) > 0
    assert model.contexts[1].tools == []
    assert policy_views[0]
    assert policy_views[1] == ()
    first_system = [
        message.content
        for message in model.contexts[0].messages
        if isinstance(message, SystemMessage)
    ]
    final_system = [
        message.content
        for message in model.contexts[1].messages
        if isinstance(message, SystemMessage)
    ]
    assert any("2 model turns remain" in content for content in first_system)
    assert any("Available tools: none." in content for content in final_system)
    assert any("verified outcome" in content for content in final_system)
    assert not any(
        "model turns remain" in message.content
        for message in harness.messages
        if isinstance(message, SystemMessage)
    )
    budget_events = [
        record
        for record in read_trace(trace_path)
        if record["type"] == "turn_budget_applied"
    ]
    assert [record["data"]["mode"] for record in budget_events] == [
        "warning",
        "finalize",
    ]
    assert all("prompt" not in record["data"] for record in budget_events)
    agent_end = next(
        record for record in read_trace(trace_path) if record["type"] == "agent_end"
    )
    assert agent_end["data"]["turns_used"] == 2
    assert agent_end["data"]["max_turns"] == 2
    assert harness.agent.last_run is not None
    assert harness.agent.last_run.end_reason == "completed"


def test_final_turn_policy_blocks_fabricated_tool_call_and_keeps_turn_limit(
    tmp_path,
) -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="shell-1",
                        name="shell_command",
                        arguments={"command": "echo should-not-run"},
                    )
                ],
                stop_reason="tool_use",
            )
        ]
    )
    harness = CodingHarness(
        model=model,
        workspace=tmp_path,
        memory_path=None,
        max_turns=1,
    )

    with pytest.raises(TurnLimitError):
        asyncio.run(harness.prompt("final answer only"))

    result = next(
        message
        for message in harness.messages
        if isinstance(message, ToolResultMessage)
    )
    assert result.is_error is True
    assert result.metadata["blocked"] is True
    assert "final turn" in result.content.lower()


def test_base_harness_does_not_apply_coding_finalization(tmp_path) -> None:
    from evopi.core.tool import Tool
    from evopi.harness import BaseHarness

    model = ScriptedModel([AssistantMessage(content="done", stop_reason="stop")])
    harness = BaseHarness(model=model, max_turns=1)
    harness.register_tool(
        Tool(
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "ok",
        )
    )

    asyncio.run(harness.prompt("plain base harness"))

    assert [tool.name for tool in model.contexts[0].tools] == ["echo"]
    assert not any(
        "model turns remain" in message.content
        for message in model.contexts[0].messages
        if isinstance(message, SystemMessage)
    )
