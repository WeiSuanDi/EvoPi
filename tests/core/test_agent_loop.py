from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from evopi.core.agent import Agent
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent, TextDelta
from evopi.core.tool import Tool, ToolCall


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
    assert [event.type for event in events].count("model_start") == 2
    assert events[-1].type == "agent_end"


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
