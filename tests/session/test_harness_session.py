from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage, SystemMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent, TextDelta
from evopi.harness.base import BaseHarness
from evopi.session import (
    CheckpointEntry,
    MessageEntry,
    RunEndEntry,
    SessionManager,
    SessionPersistenceError,
)
from evopi.trace.reader import read_trace


class RecordingModel:
    def __init__(
        self,
        *messages: AssistantMessage,
        name: str = "recording-model",
    ) -> None:
        self.name = name
        self._messages = iter(messages)
        self.contexts: list[AgentContext] = []
        self.calls = 0

    async def stream(
        self, context: AgentContext
    ) -> AsyncIterator[ModelStreamEvent]:
        self.calls += 1
        self.contexts.append(context)
        yield ModelComplete(message=next(self._messages))


class PartialFailureModel:
    name = "partial-failure"

    async def stream(
        self, context: AgentContext
    ) -> AsyncIterator[ModelStreamEvent]:
        yield TextDelta(delta="partial")
        raise RuntimeError("provider stopped")


def test_harness_persists_multiple_runs_and_restores_current_runtime(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    first_session = SessionManager.create(workspace, root=root)
    session_path = first_session.session_path
    assert session_path is not None
    first = BaseHarness(
        model=RecordingModel(
            AssistantMessage(content="first answer", stop_reason="stop"),
            name="model-v1",
        ),
        system_prompt="system-v1",
        session_manager=first_session,
    )

    asyncio.run(first.prompt("first question"))
    session_id = first.session.session_id
    first.close()

    restored_session = SessionManager.open(
        session_path,
        workspace=workspace,
        root=root,
    )
    second_model = RecordingModel(
        AssistantMessage(content="second answer", stop_reason="stop"),
        name="model-v2",
    )
    trace_path = tmp_path / "restored-trace.jsonl"
    second = BaseHarness(
        model=second_model,
        system_prompt="system-v2",
        trace_path=trace_path,
        session_manager=restored_session,
    )

    assert isinstance(second.messages[0], SystemMessage)
    assert second.messages[0].content == "system-v2"
    assert [message.content for message in second.messages[1:]] == [
        "first question",
        "first answer",
    ]

    asyncio.run(second.prompt("second question"))

    assert second.session.session_id == session_id
    assert [message.content for message in second_model.contexts[0].messages] == [
        "system-v2",
        "first question",
        "first answer",
        "second question",
    ]
    assert any(
        "model" in warning and "system_prompt_sha256" in warning
        for warning in second.session.recovery_info.warnings
    )
    entries = second.session.entries
    assert sum(isinstance(entry, RunEndEntry) for entry in entries) == 2
    assert sum(isinstance(entry, CheckpointEntry) for entry in entries) == 2
    records = list(read_trace(trace_path))
    session_start = next(
        record for record in records if record["type"] == "session_start"
    )
    assert session_start["data"]["session_id"] == session_id
    assert any(
        "runtime differs" in warning
        for warning in session_start["data"]["warnings"]
    )
    assert any(record["type"] == "session_checkpoint" for record in records)
    second.close()


def test_failed_model_attempt_is_trace_only_but_run_end_is_checkpointed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    trace_path = tmp_path / "failure.jsonl"
    harness = BaseHarness(
        model=PartialFailureModel(),
        trace_path=trace_path,
        session_manager=manager,
    )

    with pytest.raises(RuntimeError, match="provider stopped"):
        asyncio.run(harness.prompt("fail after partial output"))

    persisted_messages = [
        entry.message
        for entry in manager.entries
        if isinstance(entry, MessageEntry)
    ]
    assert [message.role for message in persisted_messages] == ["user"]
    assert isinstance(manager.entries[-2], RunEndEntry)
    assert manager.entries[-2].reason == "error"
    assert isinstance(manager.entries[-1], CheckpointEntry)
    failed_message = next(
        record
        for record in read_trace(trace_path)
        if record["type"] == "message_end"
        and record["data"].get("committed") is False
    )
    assert failed_message["data"]["message"]["content"] == "partial"
    harness.close()


def test_session_log_failure_prevents_model_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    model = RecordingModel(
        AssistantMessage(content="must not run", stop_reason="stop")
    )
    harness = BaseHarness(model=model, session_manager=manager)
    events: list[CoreEvent] = []
    harness.subscribe(events.append)

    def fail_run_start(**_kwargs: object) -> None:
        raise SessionPersistenceError("disk unavailable")

    monkeypatch.setattr(manager, "append_run_start", fail_run_start)

    with pytest.raises(SessionPersistenceError, match="disk unavailable"):
        asyncio.run(harness.prompt("do not execute"))

    assert model.calls == 0
    session_error = next(event for event in events if event.type == "session_error")
    assert session_error.data["recoverable"] is False
    harness.close()


def test_checkpoint_failure_warns_without_failing_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    harness = BaseHarness(
        model=RecordingModel(
            AssistantMessage(content="completed", stop_reason="stop")
        ),
        session_manager=manager,
    )
    events: list[CoreEvent] = []
    harness.subscribe(events.append)

    def fail_checkpoint(*_args: object, **_kwargs: object) -> str:
        raise SessionPersistenceError("checkpoint disk unavailable")

    monkeypatch.setattr(
        "evopi.session.session.write_checkpoint",
        fail_checkpoint,
    )

    answer = asyncio.run(harness.prompt("finish despite checkpoint failure"))

    assert answer.content == "completed"
    assert harness.state.end_reason == "completed"
    assert isinstance(manager.entries[-1], RunEndEntry)
    session_error = next(event for event in events if event.type == "session_error")
    assert session_error.data["operation"] == "checkpoint"
    assert session_error.data["recoverable"] is True
    assert "checkpoint disk unavailable" in session_error.data["error"]
    harness.close()
