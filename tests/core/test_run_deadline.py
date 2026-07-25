"""Tests for Run deadline via AgentLoop."""

import asyncio

import pytest

from evopi.core.agent import Agent
from evopi.core.agent_loop import AgentLoop
from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage
from evopi.core.model import Model
from evopi.core.run import AgentEndReason
from evopi.core.stream import ModelComplete
from evopi.core.tool import ToolCall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _context() -> AgentContext:
    return AgentContext(messages=[], tools=[])


class _DelayModel(Model):
    """Model that returns a text response after a configurable delay."""

    def __init__(self, content: str = "done", delay: float = 0.0) -> None:
        self._content = content
        self._delay = delay

    @property
    def name(self) -> str:
        return "test-model"

    def stream(self, context: AgentContext):
        async def _stream():
            if self._delay:
                await asyncio.sleep(self._delay)
            yield ModelComplete(
                message=AssistantMessage(content=self._content, stop_reason="stop")
            )

        return _stream()


class _ToolUseThenTextModel(Model):
    """First turn: tool call. Second turn: text response."""

    def __init__(self, tool_name: str = "noop", delay: float = 0.0) -> None:
        self._tool_name = tool_name
        self._delay = delay
        self._call_count = 0

    @property
    def name(self) -> str:
        return "test-model"

    def stream(self, context: AgentContext):
        self._call_count += 1

        async def _stream():
            if self._call_count == 1:
                yield ModelComplete(
                    message=AssistantMessage(
                        content="",
                        stop_reason="tool_use",
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name=self._tool_name,
                                arguments={},
                            )
                        ],
                    )
                )
            else:
                if self._delay:
                    await asyncio.sleep(self._delay)
                yield ModelComplete(
                    message=AssistantMessage(content="final", stop_reason="stop")
                )

        return _stream()


# ---------------------------------------------------------------------------
# AgentLoop deadline validation
# ---------------------------------------------------------------------------

def test_agent_loop_rejects_non_positive_deadline() -> None:
    with pytest.raises(ValueError, match="deadline must be positive"):
        AgentLoop(deadline=0)

    with pytest.raises(ValueError, match="deadline must be positive"):
        AgentLoop(deadline=-1.0)


def test_agent_loop_defaults_deadline_to_none() -> None:
    loop = AgentLoop()
    assert loop.deadline is None


# ---------------------------------------------------------------------------
# AgentLoop with deadline — basic scenarios
# ---------------------------------------------------------------------------

def test_deadline_not_triggered_when_run_completes_quickly() -> None:
    """A fast run completes normally even with a generous deadline."""
    loop = AgentLoop(max_turns=5, deadline=60.0)

    async def run() -> None:
        result = await loop.run_with_result(
            model=_DelayModel(content="done", delay=0.01),
            context=_context(),
        )
        assert result.end_reason == "completed"

    asyncio.run(run())


def test_deadline_exceeded_captured_in_run_result() -> None:
    """When the deadline fires during a long model call, the end_reason
    reflects deadline_exceeded even though the model produced a response."""
    loop = AgentLoop(max_turns=5, deadline=0.05)

    async def run() -> None:
        result = await loop.run_with_result(
            model=_DelayModel(content="slow response", delay=1.0),
            context=_context(),
        )
        # The model completed turn 1 but deadline fired → end_reason is deadline_exceeded
        assert result.end_reason == "deadline_exceeded"
        # The model's own response is preserved as the final message
        assert result.message.content == "slow response"

    asyncio.run(run())


def test_deadline_during_multi_turn_run() -> None:
    """Deadline fires between turns in a tool-using run."""
    loop = AgentLoop(max_turns=5, deadline=0.1)

    async def run() -> None:
        ctx = _context()
        # Register a tool so the tool call succeeds
        from evopi.core.tool import Tool
        ctx.tools.append(
            Tool(name="noop", description="d", parameters={}, handler=lambda: "ok")
        )
        result = await loop.run_with_result(
            model=_ToolUseThenTextModel(tool_name="noop", delay=10.0),
            context=ctx,
        )
        # Turn 1 (tool call) completed quickly, deadline fires during turn 2's model call
        assert result.end_reason == "deadline_exceeded"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Agent with deadline
# ---------------------------------------------------------------------------

def test_agent_accepts_deadline_parameter() -> None:
    agent = Agent(
        model=_DelayModel(content="done"),
        deadline=30.0,
    )
    assert agent._loop.deadline == 30.0


def test_agent_with_long_deadline_completes_normally() -> None:
    agent = Agent(
        model=_DelayModel(content="hello", delay=0.01),
        deadline=30.0,
    )

    async def run() -> None:
        result = await agent.prompt("test")
        assert result.content == "hello"

    asyncio.run(run())


def test_agent_with_short_deadline_reports_correct_end_reason() -> None:
    """Agent.last_run.end_reason reflects deadline_exceeded."""
    agent = Agent(
        model=_DelayModel(content="late", delay=1.0),
        deadline=0.05,
    )

    async def run() -> None:
        await agent.prompt("test")
        assert agent.last_run is not None
        assert agent.last_run.end_reason == "deadline_exceeded"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# AgentEndReason covers deadline_exceeded
# ---------------------------------------------------------------------------

def test_agent_end_reason_includes_deadline_exceeded() -> None:
    """Verify the Literal type includes 'deadline_exceeded'."""
    reason: AgentEndReason = "deadline_exceeded"
    assert reason == "deadline_exceeded"
