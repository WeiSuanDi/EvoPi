"""Tests for the SubAgent module."""

from __future__ import annotations

import asyncio

import pytest

from evopi.core.messages import AssistantMessage, UserMessage
from evopi.core.stream import ModelComplete
from evopi.core.tool import Tool
from evopi.subagents import (
    SubAgentError,
    SubAgentManager,
    SubAgentResult,
    SubAgentScope,
    validate_subagent_result,
)


# ---------------------------------------------------------------------------
# Fake model for deterministic tests
# ---------------------------------------------------------------------------

class _EchoModel:
    """Returns a fixed assistant message — never calls tools."""
    name = "echo"

    def __init__(self, content: str = "done") -> None:
        self._content = content

    def stream(self, context):
        async def _stream():
            yield ModelComplete(
                message=AssistantMessage(
                    content=self._content,
                    stop_reason="stop",
                ),
            )
        return _stream()


# ---------------------------------------------------------------------------
# SubAgentScope
# ---------------------------------------------------------------------------

def test_scope_defaults() -> None:
    scope = SubAgentScope()
    assert scope.system_prompt == ""
    assert scope.messages == []
    assert scope.tool_names == []
    assert scope.max_turns == 5


def test_scope_restrict_tightens_limits() -> None:
    scope = SubAgentScope(max_turns=10)
    restricted = scope.restrict(max_turns=3)
    assert restricted.max_turns == 3
    assert scope.max_turns == 10  # original unchanged


def test_scope_restrict_preserves_fields() -> None:
    scope = SubAgentScope(
        system_prompt="be helpful",
        messages=[UserMessage(content="hi")],
        tool_names=["read_file"],
    )
    r = scope.restrict()
    assert r.system_prompt == "be helpful"
    assert len(r.messages) == 1
    assert r.tool_names == ["read_file"]


# ---------------------------------------------------------------------------
# SubAgentResult / validation
# ---------------------------------------------------------------------------

def test_result_success() -> None:
    r = SubAgentResult(content="ok", success=True, end_reason="completed")
    assert r.success
    assert r.end_reason == "completed"


def test_validate_success_passes_through() -> None:
    r = SubAgentResult(content="hello", end_reason="completed")
    v = validate_subagent_result(r)
    assert v.success
    assert v.content == "hello"


def test_validate_error_marks_failure() -> None:
    r = SubAgentResult(content="boom", end_reason="error")
    v = validate_subagent_result(r)
    assert not v.success
    assert "error" in v.content


def test_validate_truncates_long_content() -> None:
    r = SubAgentResult(content="x" * 500, end_reason="completed")
    v = validate_subagent_result(r, max_output_chars=10)
    assert len(v.content) < 50
    assert "truncated" in v.content


# ---------------------------------------------------------------------------
# SubAgentManager
# ---------------------------------------------------------------------------

def test_manager_runs_simple_task() -> None:
    manager = SubAgentManager(_EchoModel("task done"))
    scope = SubAgentScope(
        system_prompt="helper",
        messages=[UserMessage(content="do it")],
    )
    result = asyncio.run(manager.run(scope))
    assert result.success
    assert result.content == "task done"
    assert result.end_reason == "completed"


def test_manager_respects_max_turns() -> None:
    manager = SubAgentManager(_EchoModel("ok"), tools=[])
    scope = SubAgentScope(
        messages=[UserMessage(content="x")],
        max_turns=1,
    )
    result = asyncio.run(manager.run(scope))
    assert result.turns_used == 1


def test_manager_rejects_empty_messages() -> None:
    manager = SubAgentManager(_EchoModel())
    scope = SubAgentScope()
    with pytest.raises(SubAgentError, match="at least one message"):
        asyncio.run(manager.run(scope))


def test_manager_resolves_allowed_tools() -> None:
    tool = Tool(name="read", description="d", parameters={}, handler=lambda: "ok")
    manager = SubAgentManager(_EchoModel(), tools=[tool])
    scope = SubAgentScope(
        messages=[UserMessage(content="read something")],
        tool_names=["read", "nonexistent"],  # nonexistent is silently dropped
    )
    result = asyncio.run(manager.run(scope))
    assert result.success


def test_manager_sets_task_id() -> None:
    manager = SubAgentManager(_EchoModel("ok"))
    scope = SubAgentScope(messages=[UserMessage(content="hi")])
    result = asyncio.run(manager.run(scope, task_id="task-1"))
    assert result.metadata.get("task_id") == "task-1"


# ---------------------------------------------------------------------------
# Integration: sub-agent with tool calls
# ---------------------------------------------------------------------------

def test_manager_counts_tool_calls() -> None:
    """SubAgentManager counts tool calls in the child's messages."""
    # A model that returns a text response (no tools) → 0 tool calls
    manager = SubAgentManager(_EchoModel("no tools used"))
    scope = SubAgentScope(messages=[UserMessage(content="hi")])
    result = asyncio.run(manager.run(scope))
    assert result.tool_calls_made == 0
