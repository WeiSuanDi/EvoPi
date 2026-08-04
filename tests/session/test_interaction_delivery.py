"""Delivered interactions persist through the committed-message Session path.

Only delivered input becomes a normal committed ``UserMessage`` and therefore
a Session fact; queued or cleared content never reaches the Session and no
Session schema migration occurs.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage, UserMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.harness.base import BaseHarness
from evopi.session import SessionManager


class GatedModel:
    name = "gated"

    def __init__(
        self,
        messages: list[AssistantMessage],
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._messages = iter(messages)
        self._started = started
        self._release = release

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        del context
        message = next(self._messages)
        if self._started is not None:
            self._started.set()
        if self._release is not None:
            await self._release.wait()
        yield ModelComplete(message=message)


def _delivered_user_messages(
    messages: list[object], content: str
) -> list[UserMessage]:
    return [
        message
        for message in messages
        if isinstance(message, UserMessage) and message.content == content
    ]


def test_delivered_interactions_persist_once_and_restore_with_metadata() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        model = GatedModel(
            [
                AssistantMessage(content="one", stop_reason="stop"),
                AssistantMessage(content="two", stop_reason="stop"),
            ],
            started=started,
            release=release,
        )
        session = SessionManager.in_memory()
        harness = BaseHarness(model=model, session_manager=session)
        task = asyncio.create_task(harness.prompt("initial"))
        await started.wait()
        receipt = await harness.steer("delivered text")
        release.set()
        await task

        delivered = _delivered_user_messages(session.messages, "delivered text")
        assert len(delivered) == 1
        metadata = delivered[0].metadata["interaction"]
        assert metadata["input_id"] == receipt.input_id
        assert metadata["kind"] == "steer"
        assert metadata["origin"] == "api"
        assert metadata["schema_version"] == 1

        # a fresh Harness on the same Session restores delivered input as a
        # normal UserMessage with its interaction metadata
        restorer = BaseHarness(model=GatedModel([]), session_manager=session)
        restored = _delivered_user_messages(restorer.agent.messages, "delivered text")
        assert len(restored) == 1
        assert restored[0].metadata["interaction"]["kind"] == "steer"
        assert restored[0].metadata["interaction"]["input_id"] == receipt.input_id

    asyncio.run(scenario())


def test_queued_and_cleared_interactions_never_enter_session() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        model = GatedModel(
            [AssistantMessage(content="never", stop_reason="stop")],
            started=started,
            release=release,
        )
        session = SessionManager.in_memory()
        harness = BaseHarness(model=model, session_manager=session)
        task = asyncio.create_task(harness.prompt("initial"))
        await started.wait()
        await harness.steer("undelivered secret")
        harness.abort()
        await task

        contents = [message.content for message in session.messages]
        assert "initial" in contents
        assert "undelivered secret" not in contents

    asyncio.run(scenario())


def test_persistent_session_schema_stays_v4_with_interactions(tmp_path: Path) -> None:
    async def scenario(session: SessionManager) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        model = GatedModel(
            [
                AssistantMessage(content="one", stop_reason="stop"),
                AssistantMessage(content="two", stop_reason="stop"),
            ],
            started=started,
            release=release,
        )
        harness = BaseHarness(model=model, session_manager=session)
        task = asyncio.create_task(harness.prompt("initial"))
        await started.wait()
        await harness.steer("persisted steer")
        release.set()
        await task
        harness.close()

    session = SessionManager.create(
        workspace=tmp_path / "workspace",
        root=tmp_path / "session-root",
    )
    asyncio.run(scenario(session))
    assert session.session_path is not None
    first_record = json.loads(
        session.session_path.read_text(encoding="utf-8").splitlines()[0]
    )
    assert first_record["schema_version"] == 4
