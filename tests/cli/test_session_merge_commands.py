"""REPL commands for Session branch navigation and merge."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from evopi.cli.commands import handle_slash_command
from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage, UserMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.harness import BaseHarness
from evopi.session import SessionManager, build_runtime_fingerprint


class NoCallModel:
    name = "no-call"
    context_window = 100_000

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        raise AssertionError("manual merge must not call the model")
        yield ModelComplete(  # pragma: no cover
            message=AssistantMessage(content="", stop_reason="stop")
        )


def _fingerprint():
    return build_runtime_fingerprint(
        harness="test",
        model="test",
        system_prompt="",
        tools=[],
        policies=[],
    )


def _append_run(session: SessionManager, content: str) -> str:
    run_id = uuid4().hex
    session.append_run_start(run_id=run_id, runtime_fingerprint=_fingerprint())
    session.append_message(run_id=run_id, message=UserMessage(content=content))
    session.append_run_end(run_id=run_id, reason="completed")
    assert session.leaf_id is not None
    return session.leaf_id


def test_merge_command_accepts_source_prefix_and_manual_summary() -> None:
    session = SessionManager.in_memory()
    common = _append_run(session, "shared")
    session.branch(from_entry_id=common, branch_name="experiment")
    source = _append_run(session, "source")
    session.switch_leaf(common)
    _append_run(session, "target")
    harness = BaseHarness(model=NoCallModel(), session_manager=session)

    asyncio.run(
        handle_slash_command(
            harness,
            f"/merge {source[:12]} reusable branch conclusion",
        )
    )

    assert session.messages[-1].metadata["session_merge_summary"] is True
    assert "reusable branch conclusion" in session.messages[-1].content


def test_switch_command_accepts_displayed_leaf_prefix() -> None:
    session = SessionManager.in_memory()
    first = _append_run(session, "first")
    session.branch(from_entry_id=first, branch_name="other")
    other = _append_run(session, "other")
    session.switch_leaf(first)
    harness = BaseHarness(model=NoCallModel(), session_manager=session)

    asyncio.run(handle_slash_command(harness, f"/switch {other[:16]}"))

    assert [message.content for message in session.messages] == ["first", "other"]
