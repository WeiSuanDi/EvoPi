from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from evopi.core.agent import Agent
from evopi.core.agent_loop import TurnLimitError
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent, TextDelta
from evopi.core.tool import Tool, ToolCall, ToolResult


class ScriptedModel:
    name = "scripted"

    def __init__(self, messages: list[AssistantMessage]) -> None:
        self._messages = iter(messages)
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context)
        message = next(self._messages)
        if message.content:
            yield TextDelta(delta=message.content)
        yield ModelComplete(message=message)


def test_agent_runs_model_tool_model_cycle() -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="I will add the numbers.",
                tool_calls=[ToolCall(id="call-1", name="add", arguments={"a": 2, "b": 3})],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="The answer is 5.", stop_reason="stop"),
        ]
    )
    tool = Tool(
        name="add",
        description="Add two integers",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        handler=lambda a, b: str(a + b),
    )
    events: list[CoreEvent] = []
    agent = Agent(model=model, system_prompt="Be concise", tools=[tool])
    agent.subscribe(events.append)

    answer = asyncio.run(agent.prompt("What is 2 + 3?"))

    assert answer.content == "The answer is 5."
    assert len(model.contexts) == 2
    assert isinstance(agent.messages[-2], ToolResultMessage)
    assert agent.messages[-2].content == "5"
    assert [event.type for event in events].count("message_update") == 2
    assert events[-1].type == "agent_end"
    assert events[-1].data["reason"] == "completed"
    assert agent.last_run is not None
    assert agent.last_run.end_reason == "completed"


def test_missing_tool_is_returned_to_model_as_error() -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="call-1", name="missing", arguments={})],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="I could not run that tool.", stop_reason="stop"),
        ]
    )
    agent = Agent(model=model)

    asyncio.run(agent.prompt("Try the missing tool"))

    result = next(message for message in agent.messages if isinstance(message, ToolResultMessage))
    assert result.is_error is True
    assert "not found" in result.content


def test_single_terminating_tool_skips_follow_up_model_call() -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="The tool result is the final artifact.",
                tool_calls=[ToolCall(id="call-1", name="finish", arguments={})],
                stop_reason="tool_use",
            )
        ]
    )
    tool = Tool(
        name="finish",
        description="Finish the run",
        parameters={"type": "object", "properties": {}},
        handler=lambda: ToolResult(content="artifact", terminate=True),
    )
    events: list[CoreEvent] = []
    agent = Agent(model=model, tools=[tool])
    agent.subscribe(events.append)

    answer = asyncio.run(agent.prompt("finish"))

    assert answer.content == "The tool result is the final artifact."
    assert len(model.contexts) == 1
    assert agent.last_run is not None
    assert agent.last_run.end_reason == "terminated"
    assert events[-1].data["reason"] == "terminated"
    assert [event.type for event in events].count("tool_execution_end") == 1
    result = next(message for message in agent.messages if isinstance(message, ToolResultMessage))
    assert result.terminate is True


def test_tool_batch_terminates_only_when_every_final_result_agrees() -> None:
    executed: list[str] = []

    def finish(value: str) -> ToolResult:
        executed.append(value)
        return ToolResult(content=value, terminate=True)

    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(id="call-1", name="finish", arguments={"value": "first"}),
                    ToolCall(id="call-2", name="finish", arguments={"value": "second"}),
                ],
                stop_reason="tool_use",
            )
        ]
    )
    tool = Tool(
        name="finish",
        description="Finish one item",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=finish,
    )
    agent = Agent(model=model, tools=[tool])

    asyncio.run(agent.prompt("finish both"))

    assert executed == ["first", "second"]
    assert len(model.contexts) == 1
    assert agent.last_run is not None
    assert agent.last_run.end_reason == "terminated"


def test_mixed_tool_batch_continues_to_summary() -> None:
    def maybe_finish(value: str) -> ToolResult:
        return ToolResult(content=value, terminate=value == "first")

    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(id="call-1", name="work", arguments={"value": "first"}),
                    ToolCall(id="call-2", name="work", arguments={"value": "second"}),
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="Both results were handled.", stop_reason="stop"),
        ]
    )
    tool = Tool(
        name="work",
        description="Work on one item",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=maybe_finish,
    )
    agent = Agent(model=model, tools=[tool])

    answer = asyncio.run(agent.prompt("work"))

    assert answer.content == "Both results were handled."
    assert len(model.contexts) == 2
    assert agent.last_run is not None
    assert agent.last_run.end_reason == "completed"


def test_should_stop_after_turn_runs_after_observer() -> None:
    order: list[str] = []
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={})],
                stop_reason="tool_use",
            )
        ]
    )
    tool = Tool(
        name="echo",
        description="Echo",
        parameters={"type": "object", "properties": {}},
        handler=lambda: "ok",
    )

    def after_turn(context, assistant, results) -> None:
        order.append("after_turn")

    def should_stop(context, assistant, results) -> bool:
        order.append("should_stop")
        return True

    agent = Agent(
        model=model,
        tools=[tool],
        after_turn=after_turn,
        should_stop_after_turn=should_stop,
    )

    asyncio.run(agent.prompt("echo"))

    assert order == ["after_turn", "should_stop"]
    assert agent.last_run is not None
    assert agent.last_run.end_reason == "terminated"


def test_turn_limit_has_structured_end_reason() -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={})],
                stop_reason="tool_use",
            )
        ]
    )
    tool = Tool(
        name="echo",
        description="Echo",
        parameters={"type": "object", "properties": {}},
        handler=lambda: "ok",
    )
    events: list[CoreEvent] = []
    agent = Agent(model=model, tools=[tool], max_turns=1)
    agent.subscribe(events.append)

    with pytest.raises(TurnLimitError):
        asyncio.run(agent.prompt("loop"))

    assert agent.last_run is not None
    assert agent.last_run.end_reason == "turn_limit"
    assert events[-1].type == "agent_end"
    assert events[-1].data["reason"] == "turn_limit"


def test_v2_events_expose_tool_and_turn_results() -> None:
    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="missing-1", name="missing", arguments={})],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="The tool failed.", stop_reason="stop"),
        ]
    )
    events: list[CoreEvent] = []
    agent = Agent(model=model)
    agent.subscribe(events.append)

    asyncio.run(agent.prompt("try it"))

    start = next(event for event in events if event.type == "tool_execution_start")
    end = next(event for event in events if event.type == "tool_execution_end")
    tool_turn = next(
        event
        for event in events
        if event.type == "turn_end" and event.data["tool_results"]
    )
    assert start.data["tool_call_id"] == "missing-1"
    assert end.data["tool_name"] == "missing"
    assert end.data["is_error"] is True
    assert tool_turn.data["tool_results"][0].tool_call_id == "missing-1"
    assert not {
        "user_message",
        "model_delta",
        "assistant_message",
        "tool_call",
        "tool_result",
        "final_message",
    }.intersection(event.type for event in events)
