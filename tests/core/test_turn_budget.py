from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from evopi.core.agent import Agent
from evopi.core.agent_loop import TurnLimitError
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import Tool, ToolCall


class ScriptedModel:
    name = "scripted"

    def __init__(self, messages: list[AssistantMessage]) -> None:
        self._messages = iter(messages)

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        del context
        yield ModelComplete(message=next(self._messages))


def _echo_tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo text",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=lambda value: value,
    )


def test_turn_events_and_run_state_expose_the_strict_budget() -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(id="echo-1", name="echo", arguments={"value": "ok"})
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="done", stop_reason="stop"),
        ]
    )
    agent = Agent(model=model, tools=[_echo_tool()], max_turns=2)
    events: list[CoreEvent] = []
    observed_current_turns: list[int] = []

    async def record(event: CoreEvent) -> None:
        events.append(event)
        if event.type == "turn_start":
            observed_current_turns.append(agent.current_turn)

    agent.subscribe(record)

    answer = asyncio.run(agent.prompt("work"))

    assert answer.content == "done"
    assert observed_current_turns == [1, 2]
    assert [
        event.data
        for event in events
        if event.type == "turn_start"
    ] == [
        {"turn": 1, "max_turns": 2, "remaining_turns": 2},
        {"turn": 2, "max_turns": 2, "remaining_turns": 1},
    ]
    assert agent.current_turn == 0
    assert agent.last_run is not None
    assert agent.last_run.turns_used == 2
    assert agent.last_run.max_turns == 2
    agent_end = next(event for event in events if event.type == "agent_end")
    assert agent_end.data["turns_used"] == 2
    assert agent_end.data["max_turns"] == 2


def test_turn_limit_preserves_consumed_budget_in_failed_run_state() -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(id="echo-1", name="echo", arguments={"value": "ok"})
                ],
                stop_reason="tool_use",
            )
        ]
    )
    agent = Agent(model=model, tools=[_echo_tool()], max_turns=1)

    with pytest.raises(TurnLimitError):
        asyncio.run(agent.prompt("work"))

    assert agent.last_run is not None
    assert agent.last_run.end_reason == "turn_limit"
    assert agent.last_run.turns_used == 1
    assert agent.last_run.max_turns == 1
