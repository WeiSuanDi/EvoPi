from __future__ import annotations

from collections.abc import AsyncIterator
import json
from pathlib import Path

import pytest

from evopi.cli.plugin import plugin_main
from evopi.coding import CodingHarness
from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.plugins import approved_plugin_entrypoints


PLUGIN = """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata
from evopi.core.tool import Tool

class Demo(Plugin):
    @property
    def meta(self):
        return PluginMetadata(name="demo", version="1.0")

    def register(self, api: PluginAPI) -> None:
        api.register_tool(Tool(
            name="plugin_ping",
            description="ping",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "pong",
        ))
"""


class _Model:
    name = "plugin-test"

    async def stream(
        self,
        context: AgentContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        yield ModelComplete(
            message=AssistantMessage(content="done", stop_reason="stop")
        )


def _candidate(
    path: Path,
    *,
    source: str = PLUGIN,
    requested_capabilities: tuple[str, ...] = (),
) -> Path:
    path.mkdir()
    (path / "plugin.py").write_text(source, encoding="utf-8")
    (path / "evopi-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "demo",
                "version": "1.0",
                "entrypoint": "plugin.py",
                "requested_capabilities": list(requested_capabilities),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_plugin_approve_binds_snapshot_and_workspace_trust(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = _candidate(workspace / "candidate")
    monkeypatch.setenv("EVOPI_HOME", str(home))

    code = plugin_main(
        "approve",
        [
            str(candidate),
            "--workspace",
            str(workspace),
            "--trust-workspace",
            "--json",
        ],
    )

    assert code == 0
    entrypoints = approved_plugin_entrypoints(workspace, home=home)
    assert len(entrypoints) == 1
    assert home / "artifacts" / "plugins" in entrypoints[0].parents


def test_project_plugin_is_not_active_without_workspace_trust(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = _candidate(workspace / "candidate")
    monkeypatch.setenv("EVOPI_HOME", str(home))

    code = plugin_main(
        "approve",
        [str(candidate), "--workspace", str(workspace), "--json"],
    )

    assert code == 0
    assert approved_plugin_entrypoints(workspace, home=home) == ()


def test_idle_reload_atomically_adds_and_removes_approved_plugin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = _candidate(workspace / "candidate")
    monkeypatch.setenv("EVOPI_HOME", str(home))
    harness = CodingHarness(model=_Model(), workspace=workspace)
    assert "plugin_ping" not in harness.capabilities.tool_names

    assert (
        plugin_main(
            "approve",
            [
                str(candidate),
                "--workspace",
                str(workspace),
                "--trust-workspace",
                "--json",
            ],
        )
        == 0
    )
    capabilities = harness.reload_plugins()
    assert "plugin_ping" in capabilities.tool_names
    assert "`plugin_ping`" in harness.system_prompt

    assert plugin_main("deny", [str(candidate), "--json"]) == 0
    capabilities = harness.reload_plugins()
    assert "plugin_ping" not in capabilities.tool_names
    harness.close()


@pytest.mark.parametrize(
    ("requested_capabilities", "should_load"),
    [
        (("override_tool:read_file",), True),
        ((), False),
    ],
)
def test_builtin_tool_override_requires_approved_manifest_capability(
    tmp_path: Path,
    monkeypatch,
    requested_capabilities: tuple[str, ...],
    should_load: bool,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = PLUGIN.replace('name="plugin_ping"', 'name="read_file"')
    candidate = _candidate(
        workspace / "candidate",
        source=source,
        requested_capabilities=requested_capabilities,
    )
    monkeypatch.setenv("EVOPI_HOME", str(home))
    assert (
        plugin_main(
            "approve",
            [
                str(candidate),
                "--workspace",
                str(workspace),
                "--trust-workspace",
                "--json",
            ],
        )
        == 0
    )
    paths = list(approved_plugin_entrypoints(workspace, home=home))

    if not should_load:
        with pytest.raises(ValueError, match="already registered"):
            CodingHarness(model=_Model(), workspace=workspace, plugin_paths=paths)
        return

    harness = CodingHarness(model=_Model(), workspace=workspace, plugin_paths=paths)
    tool = harness.tools.registry.require("read_file")
    assert tool.metadata["plugin_source"] == "demo"
    harness.close()
