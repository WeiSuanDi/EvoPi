from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage, SystemMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import Tool
from evopi.harness import BaseHarness
from evopi.plugins import (
    PLUGIN_API_VERSION,
    PluginAPI,
    PluginCommandContext,
    PluginContractError,
    filtered_event_listener,
)
from evopi.trace import read_trace


class RecordingModel:
    name = "recording"

    def __init__(self) -> None:
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context.snapshot())
        yield ModelComplete(
            message=AssistantMessage(content="done", stop_reason="stop")
        )


def _tool(name: str, effect: str) -> Tool:
    return Tool(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        handler=lambda: name,
        metadata={"effects": [effect]},
    )


def _write_runtime_plugin(path: Path) -> Path:
    plugin_path = path / "runtime_plugin.py"
    plugin_path.write_text(
        """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class RuntimePlugin(Plugin):
    @property
    def meta(self):
        return PluginMetadata(name="runtime-plugin", version="1.0")

    def register(self, api: PluginAPI):
        async def plan(args, context):
            assert context.command_name == "/plan"
            assert args == "on"
            api.tools.set_active(["read_file"], scope="session")

        api.register_command(
            "plan",
            plan,
            description="Enable planning",
            usage="/plan on",
        )
        api.register_prompt_fragment(
            "mode",
            lambda context: "Planning guidance" if "read_file" in context.active_tools else None,
        )
""",
        encoding="utf-8",
    )
    return plugin_path


def test_plugin_api_v1_keeps_legacy_registration_shape() -> None:
    seen: list[tuple[str, str]] = []

    async def handler(args: str, context: PluginCommandContext) -> None:
        seen.append((args, context.command_name))

    api = PluginAPI("example", "1.0")
    api.register_command(
        "/hello",
        handler,
        description="Say hello",
        usage="/hello NAME",
    )
    tool = api.register_tool(
        Tool(
            name="legacy",
            description="Legacy tool",
            parameters={},
            handler=lambda: "ok",
        )
    )

    assert PLUGIN_API_VERSION == 1
    assert api.commands[0][0] == "/hello"
    assert api.commands[0].description == "Say hello"
    assert api.commands[0].usage == "/hello NAME"
    assert tool.metadata["effects"] == ["unknown"]


def test_plugin_command_controls_active_tools_and_prompt_fragment(
    tmp_path: Path,
) -> None:
    model = RecordingModel()
    harness = BaseHarness(
        model=model,
        plugin_paths=[_write_runtime_plugin(tmp_path)],
    )
    harness.register_tool(_tool("read_file", "read"))
    harness.register_tool(_tool("write_file", "write"))

    async def run() -> None:
        handled = await harness.dispatch_plugin_command("/plan on")
        assert handled is True
        await harness.prompt("inspect")

    asyncio.run(run())

    assert harness.plugin_commands[0].name == "/plan"
    assert harness.plugin_commands[0].description == "Enable planning"
    assert [tool.name for tool in model.contexts[0].tools] == ["read_file"]
    assert any(
        message.role == "system" and message.content == "Planning guidance"
        for message in model.contexts[0].messages
    )


def test_run_scoped_tool_override_is_cleared_after_prompt(tmp_path: Path) -> None:
    plugin_path = tmp_path / "run_scope.py"
    plugin_path.write_text(
        """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class RuntimePlugin(Plugin):
    @property
    def meta(self):
        return PluginMetadata(name="run-scope")

    def register(self, api: PluginAPI):
        async def restrict(args, context):
            api.tools.set_active(["read_file"], scope="run")
        api.register_command("restrict", restrict)
""",
        encoding="utf-8",
    )
    model = RecordingModel()
    harness = BaseHarness(model=model, plugin_paths=[plugin_path])
    harness.register_tool(_tool("read_file", "read"))
    harness.register_tool(_tool("write_file", "write"))

    async def run() -> None:
        await harness.dispatch_plugin_command("/restrict")
        await harness.prompt("first")
        await harness.prompt("second")

    asyncio.run(run())

    assert [tool.name for tool in model.contexts[0].tools] == ["read_file"]
    assert [tool.name for tool in model.contexts[1].tools] == [
        "read_file",
        "write_file",
    ]


def test_active_tool_overrides_intersect_and_reject_unknown_names(
    tmp_path: Path,
) -> None:
    paths: list[Path] = []
    for plugin_name, command_name, tool_name in (
        ("reader", "reader", "read_file"),
        ("writer", "writer", "write_file"),
    ):
        path = tmp_path / f"{plugin_name}.py"
        path.write_text(
            f"""
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class RuntimePlugin(Plugin):
    @property
    def meta(self):
        return PluginMetadata(name={plugin_name!r})

    def register(self, api: PluginAPI):
        def restrict(args, context):
            api.tools.set_active([{tool_name!r}], scope="session")
        api.register_command({command_name!r}, restrict)
""",
            encoding="utf-8",
        )
        paths.append(path)

    model = RecordingModel()
    harness = BaseHarness(model=model, plugin_paths=paths)
    harness.register_tool(_tool("read_file", "read"))
    harness.register_tool(_tool("write_file", "write"))

    async def run() -> None:
        await harness.dispatch_plugin_command("/reader")
        await harness.dispatch_plugin_command("/writer")
        with pytest.raises(PluginContractError, match="Unknown active Tool"):
            harness._plugin_apis["reader"].tools.set_active(["missing"])
        await harness.prompt("nothing is mutually active")

    asyncio.run(run())

    assert model.contexts[0].tools == []


def test_plugin_context_provider_and_command_trace_are_host_managed(
    tmp_path: Path,
) -> None:
    plugin_path = tmp_path / "context_plugin.py"
    plugin_path.write_text(
        """
from evopi.core.messages import SystemMessage
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class RuntimePlugin(Plugin):
    @property
    def meta(self):
        return PluginMetadata(name="context-plugin")

    def register(self, api: PluginAPI):
        def provider(context):
            context.messages.insert(0, SystemMessage(content="Plugin context"))
            return context
        api.register_context_provider(provider)
        api.register_command("observe", lambda raw: None)
""",
        encoding="utf-8",
    )
    trace_path = tmp_path / "trace.jsonl"
    model = RecordingModel()
    harness = BaseHarness(
        model=model,
        plugin_paths=[plugin_path],
        trace_path=trace_path,
    )

    async def run() -> None:
        assert await harness.dispatch_plugin_command("/observe") is True
        assert await harness.dispatch_plugin_command("/missing") is False
        await harness.prompt("inspect")

    asyncio.run(run())

    assert any(
        isinstance(message, SystemMessage) and message.content == "Plugin context"
        for message in model.contexts[0].messages
    )
    trace_types = [record["type"] for record in read_trace(trace_path)]
    assert "plugin_command_start" in trace_types
    assert "plugin_command_end" in trace_types


def test_plugin_event_return_value_is_observational_contract_error() -> None:
    errors: list[str] = []
    listener = filtered_event_listener(
        "agent_start",
        lambda event: {"block": True},
        plugin_name="observer",
        on_contract_error=errors.append,
    )

    result = listener(CoreEvent(type="agent_start"))
    if result is not None:
        asyncio.run(result)

    assert errors == [
        "Plugin 'observer' event handler for 'agent_start' returned a value; "
        "event handlers are observational and Policy is the only execution arbiter"
    ]


def test_reserved_host_command_cannot_be_shadowed(tmp_path: Path) -> None:
    plugin_path = tmp_path / "shadow.py"
    plugin_path.write_text(
        """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata
class ShadowPlugin(Plugin):
    @property
    def meta(self):
        return PluginMetadata(name="shadow")
    def register(self, api: PluginAPI):
        api.register_command("help", lambda raw: None)
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reserved by the host"):
        BaseHarness(
            model=RecordingModel(),
            plugin_paths=[plugin_path],
            reserved_plugin_commands=frozenset({"/help"}),
        )
