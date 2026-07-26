from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import ToolCall
from evopi.coding import CodingHarness
from evopi.plugins import NullPluginUI, initialize_plugin_candidate
from evopi.session import SessionManager


class ScriptedModel:
    name = "plan-mode-test"

    def __init__(self, messages: list[AssistantMessage]) -> None:
        self._messages = iter(messages)
        self.contexts: list[AgentContext] = []

    async def stream(
        self,
        context: AgentContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context.snapshot())
        yield ModelComplete(message=next(self._messages))


class ApprovingUI(NullPluginUI):
    async def confirm(self, title: str, message: str) -> bool:
        return True


def test_packaged_plan_mode_is_a_governed_restart_safe_plugin(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = initialize_plugin_candidate(
        "plan-mode",
        template="plan-mode",
        path=workspace / ".evopi" / "plugin-candidates" / "plan-mode",
    )
    session = SessionManager.create(workspace, root=tmp_path / "sessions")
    first = CodingHarness(
        model=ScriptedModel(
            [AssistantMessage(content="unused", stop_reason="stop")]
        ),
        workspace=workspace,
        plugin_paths=[candidate / "plugin.py"],
        session_manager=session,
    )

    asyncio.run(first.dispatch_plugin_command("/plan on"))

    session_id = first.session.session_id
    assert first.session.plugin_state("plan-mode")["enabled"] is True
    first.close()

    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={"path": "blocked.txt", "content": "blocked"},
                    )
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(
                content="The write was blocked while planning.",
                stop_reason="stop",
            ),
        ]
    )
    restored_session = SessionManager.open(
        session_id,
        workspace=workspace,
        root=tmp_path / "sessions",
    )
    restored = CodingHarness(
        model=model,
        workspace=workspace,
        plugin_paths=[candidate / "plugin.py"],
        session_manager=restored_session,
    )

    asyncio.run(restored.prompt("try a direct write"))

    assert {tool.name for tool in model.contexts[0].tools} == {
        "list_dir",
        "read_file",
    }
    assert any(
        message.role == "system" and "Plan Mode is enabled" in message.content
        for message in model.contexts[0].messages
    )
    result = next(
        message
        for message in restored.agent.messages
        if isinstance(message, ToolResultMessage)
    )
    assert result.is_error is True
    assert "Plan Mode blocks Tool effects" in result.content
    assert not (workspace / "blocked.txt").exists()

    asyncio.run(restored.dispatch_plugin_command("/execute"))
    assert restored.session.plugin_state("plan-mode")["enabled"] is True

    restored.attach_plugin_ui(ApprovingUI())
    asyncio.run(restored.dispatch_plugin_command("/execute"))

    assert restored.session.plugin_state("plan-mode")["enabled"] is False
    assert {tool.name for tool in restored.agent.tools} >= {
        "edit_file",
        "shell_command",
        "write_file",
    }
    restored.close()
