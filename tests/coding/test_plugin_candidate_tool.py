from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from evopi.coding import CodingHarness, create_plugin_candidate_tool
from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import ToolCall
from evopi.harness.confirmation import ConfirmationRequest, ConfirmationResponse
from evopi.plugins import review_plugin
from evopi.trace.reader import read_trace


class ScriptedModel:
    name = "plugin-authoring-test"
    context_window = 0

    def __init__(self, messages: list[AssistantMessage]) -> None:
        self._messages = iter(messages)

    async def stream(
        self,
        context: AgentContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        yield ModelComplete(message=next(self._messages))


@pytest.mark.parametrize("template", ["basic", "plan-mode"])
def test_plugin_candidate_tool_creates_only_a_reviewable_inactive_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template: str,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EVOPI_HOME", str(home))
    tool = create_plugin_candidate_tool(tmp_path)

    result = asyncio.run(
        tool.execute({"name": f"{template}-sample", "template": template})
    )

    target = (
        tmp_path / ".evopi" / "plugin-candidates" / f"{template}-sample"
    )
    payload = json.loads(result.content)
    assert result.is_error is False
    assert target.is_dir()
    assert payload["candidate_path"] == str(target.resolve())
    assert payload["static_check"] == "passed"
    assert len(payload["digest"]) == 64
    assert payload["next_steps"] == ["review", "approve", "reload"]
    assert review_plugin(target).passed is True
    assert tool.metadata["effects"] == ["write"]
    assert not (home / "activations.json").exists()
    assert not (home / "plugins" / "artifacts").exists()


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"name": "../escape", "template": "basic"}, "Plugin name"),
        ({"name": "demo", "template": "unknown"}, "Unknown Plugin template"),
    ],
)
def test_plugin_candidate_tool_rejects_invalid_inputs_without_escape(
    tmp_path: Path,
    arguments: dict[str, str],
    error: str,
) -> None:
    tool = create_plugin_candidate_tool(tmp_path)

    result = asyncio.run(tool.execute(arguments))

    assert result.is_error is True
    assert error in result.content
    assert not (tmp_path.parent / "escape").exists()


def test_plugin_candidate_tool_does_not_overwrite_nonempty_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / ".evopi" / "plugin-candidates" / "existing"
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    result = asyncio.run(
        create_plugin_candidate_tool(tmp_path).execute(
            {"name": "existing", "template": "basic"}
        )
    )

    assert result.is_error is True
    assert "not empty" in result.content
    assert marker.read_text(encoding="utf-8") == "keep"


def test_candidate_tool_is_default_but_respects_tool_ceiling(
    tmp_path: Path,
) -> None:
    default = CodingHarness(
        model=ScriptedModel(
            [AssistantMessage(content="done", stop_reason="stop")]
        ),
        workspace=tmp_path,
        memory_path=None,
    )
    read_only = CodingHarness(
        model=ScriptedModel(
            [AssistantMessage(content="done", stop_reason="stop")]
        ),
        workspace=tmp_path,
        memory_path=None,
        tool_names={"read_file"},
    )

    assert "create_plugin_candidate" in default.capabilities.active_tool_names
    assert "`create_plugin_candidate`" in default.system_prompt
    assert (
        "use `create_plugin_candidate` before editing"
        in default.system_prompt.lower()
    )
    assert "create_plugin_candidate" not in read_only.capabilities.active_tool_names
    assert "`create_plugin_candidate`" not in read_only.system_prompt


def test_web_search_candidate_authoring_stops_at_human_activation_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EVOPI_HOME", str(home))
    candidate_relative = Path(".evopi") / "plugin-candidates" / "web-search"
    plugin_relative = candidate_relative / "plugin.py"
    test_command = _candidate_test_command(candidate_relative)
    confirmation_tools: list[str] = []

    def approve(request: ConfirmationRequest) -> ConfirmationResponse:
        confirmation_tools.append(
            request.tool_call.name if request.tool_call is not None else "-"
        )
        return ConfirmationResponse(request_id=request.id, decision="approve")

    model = ScriptedModel(
        [
            AssistantMessage(
                content="Create the inactive scaffold.",
                tool_calls=[
                    ToolCall(
                        id="candidate-1",
                        name="create_plugin_candidate",
                        arguments={"name": "web-search", "template": "basic"},
                    )
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(
                content="Customize it incrementally.",
                tool_calls=[
                    ToolCall(
                        id="edit-1",
                        name="edit_file",
                        arguments={
                            "path": str(plugin_relative),
                            "old_text": "A PluginAPI v1 extension.",
                            "new_text": "A governed web-search extension.",
                        },
                    )
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(
                content="Run the generated candidate test.",
                tool_calls=[
                    ToolCall(
                        id="test-1",
                        name="shell_command",
                        arguments={"command": test_command},
                    )
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(
                content=(
                    "Candidate created and tested. Next, a human must run "
                    "review → approve → reload."
                ),
                stop_reason="stop",
            ),
        ]
    )
    trace_path = tmp_path / "trace.jsonl"
    harness = CodingHarness(
        model=model,
        workspace=tmp_path,
        trace_path=trace_path,
        memory_path=None,
        confirmation_handler=approve,
    )

    answer = asyncio.run(
        harness.prompt("Create a web-search Plugin candidate")
    )

    candidate = tmp_path / candidate_relative
    results = [
        message
        for message in harness.messages
        if isinstance(message, ToolResultMessage)
    ]
    assert answer.stop_reason == "stop"
    assert [item.tool_name for item in results] == [
        "create_plugin_candidate",
        "edit_file",
        "shell_command",
    ]
    assert all(not item.is_error for item in results), [
        (item.tool_name, item.content, item.metadata) for item in results
    ]
    assert confirmation_tools == ["shell_command"]
    assert "governed web-search" in (candidate / "plugin.py").read_text(
        encoding="utf-8"
    )
    assert review_plugin(candidate).passed is True
    assert not (home / "activations.json").exists()
    assert "approve" in answer.content
    records = list(read_trace(trace_path))
    assert sum(
        record["type"] == "tool_execution_start"
        and record["data"]["tool_name"] == "create_plugin_candidate"
        for record in records
    ) == 1


def _candidate_test_command(candidate: Path) -> str:
    if os.name == "nt":
        python = subprocess.list2cmdline([sys.executable])
        return f'cd /d "{candidate}" && {python} -m pytest -q'
    return (
        f"cd {shlex.quote(str(candidate))} && "
        f"{shlex.quote(sys.executable)} -m pytest -q"
    )
