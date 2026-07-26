from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import Tool
from evopi.harness import BaseHarness
from evopi.session import (
    PluginStateEntry,
    SessionManager,
    build_runtime_fingerprint,
    entry_from_dict,
    entry_to_dict,
)
from evopi.session.errors import SessionSerializationError
from evopi.session.errors import SessionPersistenceError


class RecordingModel:
    name = "recording"

    def __init__(self) -> None:
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context.snapshot())
        yield ModelComplete(
            message=AssistantMessage(content="done", stop_reason="stop")
        )


def _fingerprint():
    return build_runtime_fingerprint(
        harness="test",
        model="test",
        system_prompt="",
        tools=[],
        policies=[],
    )


def _tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=name,
        parameters={},
        handler=lambda: name,
        metadata={"effects": ["read" if name == "read_file" else "write"]},
    )


def _write_state_plugin(path: Path) -> Path:
    plugin_path = path / "state_plugin.py"
    plugin_path.write_text(
        """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class StatePlugin(Plugin):
    @property
    def meta(self):
        return PluginMetadata(name="state-plugin", version="1.0")

    def register(self, api: PluginAPI):
        def enable(args, context):
            api.state.set("mode", "plan")
            api.tools.set_active(["read_file"], scope="session")
        api.register_command("enable", enable)
        api.register_prompt_fragment(
            "state",
            lambda context: "Mode: plan" if api.state.get("mode") == "plan" else None,
        )
""",
        encoding="utf-8",
    )
    return plugin_path


def test_plugin_state_entry_round_trips_with_optional_run_id() -> None:
    entry = PluginStateEntry(
        entry_id=uuid4().hex,
        parent_id=None,
        run_id=None,
        plugin_name="plan-mode",
        plugin_version="1.0",
        key="mode",
        operation="set",
        value={"enabled": True},
    )

    restored = entry_from_dict(entry_to_dict(entry))

    assert restored == entry
    assert restored.schema_version == 3


def test_plugin_state_follows_active_session_branch() -> None:
    session = SessionManager.in_memory()
    enabled = session.append_plugin_state(
        plugin_name="plan-mode",
        plugin_version="1.0",
        key="mode",
        value="plan",
    )
    session.append_plugin_state(
        plugin_name="plan-mode",
        plugin_version="1.0",
        key="mode",
        value="execute",
    )
    assert session.plugin_state("plan-mode") == {"mode": "execute"}

    session.switch_leaf(enabled.entry_id)

    assert session.plugin_state("plan-mode") == {"mode": "plan"}


def test_plugin_state_reads_cannot_mutate_projection_without_entry() -> None:
    session = SessionManager.in_memory()
    session.append_plugin_state(
        plugin_name="sample",
        plugin_version="1.0",
        key="nested",
        value={"items": ["original"]},
    )

    detached = session.plugin_state("sample")
    detached["nested"]["items"].append("mutated")

    assert session.plugin_state("sample") == {
        "nested": {"items": ["original"]}
    }


def test_plugin_state_persists_in_checkpoint_and_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SessionManager.create(workspace, root=tmp_path / "sessions")
    session.append_plugin_state(
        plugin_name="plan-mode",
        plugin_version="1.0",
        key="mode",
        value={"enabled": True},
    )
    run_id = uuid4().hex
    session.append_run_start(run_id=run_id, runtime_fingerprint=_fingerprint())
    run_end = session.append_run_end(run_id=run_id, reason="completed")
    checkpoint = session.create_checkpoint(
        run_end=run_end,
        runtime_fingerprint=_fingerprint(),
    )
    assert checkpoint is not None
    assert checkpoint.plugin_state == {
        "plan-mode": {"mode": {"enabled": True}}
    }
    session_id = session.session_id
    session.close()

    reopened = SessionManager.open(
        session_id,
        workspace=workspace,
        root=tmp_path / "sessions",
    )

    assert reopened.plugin_state("plan-mode") == {
        "mode": {"enabled": True}
    }
    reopened.close()


def test_plugin_state_is_projected_into_persistent_fork(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SessionManager.create(workspace, root=tmp_path / "sessions")
    session.append_plugin_state(
        plugin_name="plan-mode",
        plugin_version="1.1",
        key="mode",
        value="plan",
    )
    run_id = uuid4().hex
    session.append_run_start(run_id=run_id, runtime_fingerprint=_fingerprint())
    session.append_run_end(run_id=run_id, reason="completed")

    forked = session.fork()

    assert forked.plugin_state("plan-mode") == {"mode": "plan"}
    assert forked.plugin_state_version("plan-mode") == "1.1"
    forked.close()
    session.close()


def test_plugin_state_run_id_requires_matching_open_run() -> None:
    session = SessionManager.in_memory()

    with pytest.raises(SessionPersistenceError, match="currently open Run"):
        session.append_plugin_state(
            plugin_name="plan-mode",
            plugin_version="1.0",
            key="mode",
            value="plan",
            run_id=uuid4().hex,
        )


def test_plugin_state_rejects_oversized_value() -> None:
    session = SessionManager.in_memory()

    with pytest.raises(SessionSerializationError, match="64 KiB"):
        session.append_plugin_state(
            plugin_name="large",
            plugin_version="1.0",
            key="value",
            value="x" * (64 * 1024),
        )


def test_harness_restores_plugin_state_and_session_tool_override(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugin_path = _write_state_plugin(tmp_path)
    session = SessionManager.create(workspace, root=tmp_path / "sessions")
    model = RecordingModel()
    harness = BaseHarness(
        model=model,
        plugin_paths=[plugin_path],
        session_manager=session,
    )
    harness.register_tool(_tool("read_file"))
    harness.register_tool(_tool("write_file"))

    async def first_run() -> None:
        await harness.dispatch_plugin_command("/enable")
        await harness.prompt("first")

    asyncio.run(first_run())
    assert [tool.name for tool in model.contexts[0].tools] == ["read_file"]
    session_id = harness.session.session_id
    harness.close()

    restored_session = SessionManager.open(
        session_id,
        workspace=workspace,
        root=tmp_path / "sessions",
    )
    restored_model = RecordingModel()
    restored = BaseHarness(
        model=restored_model,
        plugin_paths=[plugin_path],
        session_manager=restored_session,
    )
    restored.register_tool(_tool("read_file"))
    restored.register_tool(_tool("write_file"))

    asyncio.run(restored.prompt("second"))

    assert [tool.name for tool in restored_model.contexts[0].tools] == ["read_file"]
    assert any(
        message.role == "system" and message.content == "Mode: plan"
        for message in restored_model.contexts[0].messages
    )
    restored.close()
