from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from evopi.core.messages import (
    AssistantMessage,
    Message,
    ToolResultMessage,
    UserMessage,
)
from evopi.core.tool import ToolArgumentError, ToolCall
from evopi.session import (
    CheckpointEntry,
    MessageEntry,
    RunEndEntry,
    RunStartEntry,
    RuntimeFingerprint,
    SessionFormatError,
    SessionLockError,
    SessionManager,
    SessionPersistenceError,
    SessionRunEndReason,
    SessionSerializationError,
    build_runtime_fingerprint,
    message_from_dict,
    message_to_dict,
)


def fingerprint(*, model: str = "fake") -> RuntimeFingerprint:
    return build_runtime_fingerprint(
        harness="tests.FakeHarness",
        model=model,
        system_prompt="system",
        tools=[{"name": "read_file", "parameters": {"type": "object"}}],
        policies=[{"name": "guard", "version": "1.0.0"}],
    )


def complete_run(
    manager: SessionManager,
    *,
    content: str = "hello",
) -> tuple[str, str]:
    run_id = uuid4().hex
    start = manager.append_run_start(
        run_id=run_id,
        runtime_fingerprint=fingerprint(),
    )
    manager.append_message(run_id=run_id, message=UserMessage(content=content))
    manager.append_message(
        run_id=run_id,
        message=AssistantMessage(content=f"answer: {content}", stop_reason="stop"),
    )
    end = manager.append_run_end(run_id=run_id, reason="completed")
    checkpoint = manager.create_checkpoint(
        run_end=end,
        runtime_fingerprint=start.runtime_fingerprint,
    )
    assert checkpoint is not None
    return run_id, checkpoint.checkpoint_id


def test_message_codec_round_trips_all_persisted_roles() -> None:
    messages: list[Message] = [
        UserMessage(content="hello", metadata={"path": Path("README.md")}),
        AssistantMessage(
            content="calling",
            tool_calls=[
                ToolCall(
                    id=uuid4().hex,
                    name="read_file",
                    arguments={"path": "README.md"},
                )
            ],
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            content="contents",
            tool_call_id=uuid4().hex,
            tool_name="read_file",
            is_error=False,
            terminate=True,
        ),
    ]

    restored = [message_from_dict(message_to_dict(message)) for message in messages]

    assert [message.role for message in restored] == [
        "user",
        "assistant",
        "tool_result",
    ]
    assert restored[0].id == messages[0].id
    assert restored[0].metadata == {"path": "README.md"}
    assert isinstance(restored[1], AssistantMessage)
    assert restored[1].tool_calls[0].arguments == {"path": "README.md"}
    assert isinstance(restored[2], ToolResultMessage)
    assert restored[2].terminate is True


def test_message_codec_omits_raw_invalid_tool_argument_fragment() -> None:
    message = AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(
                id=uuid4().hex,
                name="write_file",
                argument_error=ToolArgumentError(
                    code="invalid_json",
                    message="Tool arguments are not valid JSON",
                    raw_fragment="SECRET-RAW-FRAGMENT",
                ),
            )
        ],
        stop_reason="tool_use",
    )

    encoded = message_to_dict(message)
    restored = message_from_dict(encoded)

    assert "SECRET-RAW-FRAGMENT" not in json.dumps(encoded)
    assert isinstance(restored, AssistantMessage)
    assert restored.tool_calls[0].argument_error is not None
    assert restored.tool_calls[0].argument_error.raw_fragment is None


def test_persistent_session_round_trip_and_tree_contract(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    session_id = manager.session_id
    _, checkpoint_id = complete_run(manager)
    session_path = manager.session_path
    assert session_path is not None
    manager.close()

    restored = SessionManager.open(
        session_path,
        workspace=workspace,
        root=tmp_path / "sessions",
    )

    assert restored.session_id == session_id
    assert [message.content for message in restored.messages] == [
        "hello",
        "answer: hello",
    ]
    assert restored.last_checkpoint is not None
    assert restored.last_checkpoint.checkpoint_id == checkpoint_id
    assert [type(entry) for entry in restored.entries] == [
        RunStartEntry,
        MessageEntry,
        MessageEntry,
        RunEndEntry,
        CheckpointEntry,
    ]
    assert restored.entries[0].parent_id is None
    assert all(
        current.parent_id == previous.entry_id
        for previous, current in zip(restored.entries, restored.entries[1:])
    )
    header = json.loads(session_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["schema_version"] == 3
    assert header["type"] == "session"
    restored.close()


def test_session_lock_rejects_concurrent_open(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    session_path = manager.session_path
    assert session_path is not None

    with pytest.raises(SessionLockError):
        SessionManager.open(
            session_path,
            workspace=workspace,
            root=tmp_path / "sessions",
        )

    manager.close()
    reopened = SessionManager.open(
        session_path,
        workspace=workspace,
        root=tmp_path / "sessions",
    )
    reopened.close()


def test_v1_log_is_atomically_migrated_with_backup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    manager = SessionManager.create(workspace, root=root)
    complete_run(manager)
    session_path = manager.session_path
    assert session_path is not None
    manager.close()

    records = [
        json.loads(line)
        for line in session_path.read_text(encoding="utf-8").splitlines()
    ]
    for record in records:
        record["schema_version"] = 1
    session_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    restored = SessionManager.open(session_path, workspace=workspace, root=root)
    restored.close()

    assert (session_path.parent / "session.v1.jsonl.bak").exists()
    migrated = [
        json.loads(line)
        for line in session_path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(record["schema_version"] == 3 for record in migrated)


def test_v2_log_is_atomically_migrated_with_backup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    manager = SessionManager.create(workspace, root=root)
    complete_run(manager)
    session_path = manager.session_path
    assert session_path is not None
    manager.close()

    records = [
        json.loads(line)
        for line in session_path.read_text(encoding="utf-8").splitlines()
    ]
    for record in records:
        record["schema_version"] = 2
    session_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    restored = SessionManager.open(session_path, workspace=workspace, root=root)
    restored.close()

    assert (session_path.parent / "session.v2.jsonl.bak").exists()
    migrated = [
        json.loads(line)
        for line in session_path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(record["schema_version"] == 3 for record in migrated)


def test_interrupted_run_gets_unknown_tool_result_without_execution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    run_id = uuid4().hex
    call_id = uuid4().hex
    manager.append_run_start(
        run_id=run_id,
        runtime_fingerprint=fingerprint(),
    )
    manager.append_message(
        run_id=run_id,
        message=UserMessage(content="run the tool"),
    )
    manager.append_message(
        run_id=run_id,
        message=AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(id=call_id, name="shell_command", arguments={"command": "x"})
            ],
            stop_reason="tool_use",
        ),
    )
    session_path = manager.session_path
    assert session_path is not None
    manager.close()

    restored = SessionManager.open(
        session_path,
        workspace=workspace,
        root=tmp_path / "sessions",
    )

    tool_results = [
        message
        for message in restored.messages
        if isinstance(message, ToolResultMessage)
    ]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == call_id
    assert tool_results[0].is_error is True
    assert tool_results[0].metadata["outcome"] == "unknown"
    assert restored.recovery_info.interrupted_run_id == run_id
    assert restored.recovery_info.synthesized_tool_results == 1
    assert isinstance(restored.entries[-2], RunEndEntry)
    assert restored.entries[-2].reason == "interrupted"
    assert isinstance(restored.entries[-1], CheckpointEntry)
    restored.close()


def test_truncated_final_record_is_repaired_but_interior_corruption_fails(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    complete_run(manager)
    session_path = manager.session_path
    assert session_path is not None
    manager.close()
    with session_path.open("ab") as handle:
        handle.write(b'{"type":')

    repaired = SessionManager.open(
        session_path,
        workspace=workspace,
        root=tmp_path / "sessions",
    )
    assert repaired.recovery_info.repaired_trailing_line is True
    repaired.close()

    lines = session_path.read_bytes().splitlines(keepends=True)
    session_path.write_bytes(lines[0] + b"{bad json}\n" + b"".join(lines[1:]))
    with pytest.raises(SessionFormatError, match="line 2"):
        SessionManager.open(
            session_path,
            workspace=workspace,
            root=tmp_path / "sessions",
        )


def test_corrupt_checkpoint_falls_back_to_authoritative_log(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    complete_run(manager)
    session_path = manager.session_path
    checkpoint = manager.last_checkpoint
    assert session_path is not None
    assert checkpoint is not None
    checkpoint_path = (
        session_path.parent / "checkpoints" / f"{checkpoint.checkpoint_id}.json"
    )
    manager.close()
    checkpoint_path.write_text("{}", encoding="utf-8")

    restored = SessionManager.open(
        session_path,
        workspace=workspace,
        root=tmp_path / "sessions",
    )

    assert [message.content for message in restored.messages] == [
        "hello",
        "answer: hello",
    ]
    assert restored.recovery_info.rebuilt_from_log is True
    assert any(
        "checksum mismatch" in warning
        for warning in restored.recovery_info.warnings
    )
    restored.close()


def test_workspace_mismatch_is_a_warning(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    manager = SessionManager.create(first, root=tmp_path / "sessions")
    complete_run(manager)
    session_path = manager.session_path
    assert session_path is not None
    manager.close()

    restored = SessionManager.open(
        session_path,
        workspace=second,
        root=tmp_path / "sessions",
    )

    assert restored.recovery_info.workspace_mismatch is True
    assert any("workspace differs" in item for item in restored.recovery_info.warnings)
    restored.close()


def test_non_json_metadata_breaks_authoritative_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    run_id = uuid4().hex
    manager.append_run_start(
        run_id=run_id,
        runtime_fingerprint=fingerprint(),
    )

    with pytest.raises(SessionSerializationError):
        manager.append_message(
            run_id=run_id,
            message=UserMessage(content="bad", metadata={"value": object()}),
        )
    with pytest.raises(SessionPersistenceError, match="cannot continue"):
        manager.append_run_end(run_id=run_id, reason="error")
    manager.close()


def test_list_and_continue_recent_use_the_latest_workspace_session(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = SessionManager.create(workspace, root=tmp_path / "sessions")
    complete_run(first, content="first")
    first.close()
    second = SessionManager.create(workspace, root=tmp_path / "sessions")
    complete_run(second, content="second")
    second_id = second.session_id
    second.close()

    summaries = SessionManager.list(
        workspace=workspace,
        root=tmp_path / "sessions",
    )
    assert [summary.session_id for summary in summaries][:2] == [
        second_id,
        first.session_id,
    ]

    continued = SessionManager.continue_recent(
        workspace,
        root=tmp_path / "sessions",
    )
    assert continued.session_id == second_id
    assert continued.recovery_info.reason == "continue"
    continued.close()


@pytest.mark.parametrize(
    "reason",
    ["completed", "terminated", "aborted", "error", "turn_limit"],
)
def test_every_core_end_reason_produces_an_immutable_checkpoint(
    tmp_path: Path,
    reason: SessionRunEndReason,
) -> None:
    workspace = tmp_path / reason
    workspace.mkdir()
    manager = SessionManager.create(workspace, root=tmp_path / "sessions")
    run_id = uuid4().hex
    start = manager.append_run_start(
        run_id=run_id,
        runtime_fingerprint=fingerprint(),
    )
    manager.append_message(
        run_id=run_id,
        message=UserMessage(content=reason),
    )
    run_end = manager.append_run_end(
        run_id=run_id,
        reason=reason,
    )

    checkpoint = manager.create_checkpoint(
        run_end=run_end,
        runtime_fingerprint=start.runtime_fingerprint,
    )

    assert checkpoint is not None
    assert checkpoint.last_run.reason == reason
    assert manager.session_path is not None
    checkpoint_path = (
        manager.session_path.parent
        / "checkpoints"
        / f"{checkpoint.checkpoint_id}.json"
    )
    original = checkpoint_path.read_bytes()
    assert checkpoint_path.exists()
    assert checkpoint_path.read_bytes() == original
    manager.close()
