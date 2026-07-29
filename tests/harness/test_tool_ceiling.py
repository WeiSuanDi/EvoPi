from __future__ import annotations

import asyncio

import pytest

from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete
from evopi.core.tool import Tool
from evopi.harness import BaseHarness, ToolCapability
from evopi.trace import read_trace


class _Model:
    name = "test"
    context_window = 0

    async def stream(self, context):
        yield ModelComplete(
            message=AssistantMessage(content="done", stop_reason="stop")
        )


def _tool(name: str, effect: str, *, plugin: str | None = None) -> Tool:
    metadata: dict[str, object] = {"effects": [effect]}
    if plugin is not None:
        metadata["plugin_source"] = plugin
    return Tool(
        name=name,
        description=f"{name} description",
        parameters={"type": "object", "properties": {}},
        handler=lambda: name,
        metadata=metadata,
    )


def test_tool_ceiling_intersects_with_plugin_active_overrides() -> None:
    harness = BaseHarness(model=_Model())
    harness.register_tool(_tool("read", "read"))
    harness.register_tool(_tool("write", "write", plugin="demo"))
    harness.configure_tool_ceiling(include_names={"read"})

    assert harness.capabilities.tool_names == ("read", "write")
    assert harness.capabilities.active_tool_names == ("read",)

    harness._plugin_active_overrides["demo"] = (  # ownership layer simulation
        "session",
        frozenset({"read", "write"}),
    )
    assert tuple(tool.name for tool in harness.active_tools()) == ("read",)


def test_tool_ceiling_rejects_unknown_and_mutation_while_running() -> None:
    harness = BaseHarness(model=_Model())
    harness.register_tool(_tool("read", "read"))

    with pytest.raises(ValueError, match="Unknown Tool"):
        harness.configure_tool_ceiling(include_names={"missing"})

    harness.agent._active_run = object()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="running"):
        harness.configure_tool_ceiling(include_names={"read"})


def test_capabilities_describe_registered_and_active_tools() -> None:
    harness = BaseHarness(model=_Model())
    harness.register_tool(_tool("read", "read"))
    harness.register_tool(_tool("plugin_write", "write", plugin="demo"))
    harness.configure_tool_ceiling(exclude_names={"plugin_write"})

    assert harness.capabilities.tools == (
        ToolCapability(
            name="plugin_write",
            description="plugin_write description",
            effects=("write",),
            source="plugin",
            plugin="demo",
            active=False,
        ),
        ToolCapability(
            name="read",
            description="read description",
            effects=("read",),
            source="harness",
            plugin=None,
            active=True,
        ),
    )


def test_tool_ceiling_and_runtime_fingerprint_are_traced(tmp_path) -> None:
    trace = tmp_path / "trace.jsonl"
    harness = BaseHarness(model=_Model(), trace_path=trace)
    harness.register_tool(_tool("read", "read"))
    harness.register_tool(_tool("write", "write"))
    harness.configure_tool_ceiling(include_names={"read"})

    asyncio.run(harness.prompt("go"))

    start = next(
        record for record in read_trace(trace) if record["type"] == "session_start"
    )
    assert start["data"]["active_tool_names"] == ["read"]
    assert start["data"]["runtime_fingerprint"]["tools_sha256"]
