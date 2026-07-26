"""Tests for the Plugin system v2."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from evopi.core.tool import Tool
from evopi.plugins import (
    Plugin,
    PluginAPI,
    PluginLoader,
    PluginMetadata,
    discover_plugin_paths,
    filtered_event_listener,
    load_plugin,
    wire_plugins,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace() -> Path:
    """Isolated workspace with its own global root to avoid picking up
    real ~/`.evopi/plugins/`."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "workspace"
        ws.mkdir()
        yield ws


def _root(ws: Path) -> Path:
    """Return an isolated global root for testing."""
    return ws / "global_root"


def _write_plugin(plugins_dir: Path, name: str, body: str) -> Path:
    plugins_dir.mkdir(parents=True, exist_ok=True)
    path = plugins_dir / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# PluginMetadata
# ---------------------------------------------------------------------------

def test_metadata_is_frozen() -> None:
    meta = PluginMetadata(name="test", version="2.0", description="desc")
    assert meta.name == "test"
    assert meta.version == "2.0"
    assert meta.description == "desc"
    assert meta.dependencies == ()
    with pytest.raises(Exception):
        meta.name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Plugin + PluginAPI
# ---------------------------------------------------------------------------

class _TestPlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="test", version="1.0")

    def register(self, api: PluginAPI) -> None:
        api.register_tool(Tool(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "ok",
        ))
        api.on("agent_start", lambda e: None)
        api.register_command("/test", lambda t: None)


def test_plugin_api_public_attributes() -> None:
    api = PluginAPI("my-plugin", "2.0")
    assert api.tools == []
    assert api.events == []
    assert api.commands == []
    assert api.policies == []
    assert api.policy_packs == []
    assert api.plugin_name == "my-plugin"
    assert api.plugin_version == "2.0"


def test_tool_is_tagged_with_plugin_source() -> None:
    plugin = _TestPlugin()
    api = PluginAPI(plugin.meta.name, plugin.meta.version)
    plugin.register(api)
    assert len(api.tools) == 1
    tool = api.tools[0]
    assert tool.metadata.get("plugin_source") == "test"
    assert tool.metadata.get("plugin_version") == "1.0"
    assert tool.name == "test_tool"


def test_plugin_registers_events() -> None:
    plugin = _TestPlugin()
    api = PluginAPI(plugin.meta.name, plugin.meta.version)
    plugin.register(api)
    assert len(api.events) == 1
    assert api.events[0][0] == "agent_start"


def test_plugin_registers_commands() -> None:
    plugin = _TestPlugin()
    api = PluginAPI(plugin.meta.name, plugin.meta.version)
    plugin.register(api)
    assert len(api.commands) == 1
    assert api.commands[0][0] == "/test"


def test_require_records_dependencies() -> None:
    api = PluginAPI("p", "1.0")
    api.require("other", ">=1.0")
    assert len(api._declared_deps) == 1
    assert api._declared_deps[0] == ("other", ">=1.0")


def test_filtered_event_listener_ignores_other_event_types() -> None:
    from evopi.core.events import CoreEvent

    seen: list[str] = []
    listener = filtered_event_listener(
        "agent_start",
        lambda event: seen.append(event.type),
    )

    listener(CoreEvent(type="turn_start"))
    listener(CoreEvent(type="agent_start"))

    assert seen == ["agent_start"]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

SIMPLE_PLUGIN = """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class HelloPlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="hello", version="1.0", description="Hello")

    def register(self, api: PluginAPI) -> None:
        pass
"""


def test_load_plugin_from_py_file(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "hello", SIMPLE_PLUGIN)
    plugin = load_plugin(d / "hello.py")
    assert plugin is not None
    assert plugin.meta.name == "hello"
    assert plugin.meta.version == "1.0"


def test_load_plugin_invalid_path_returns_none() -> None:
    assert load_plugin("/nonexistent/plugin.py") is None


def test_load_plugin_package_with_init(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    pkg = d / "pkg_plugin"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(SIMPLE_PLUGIN)
    plugin = load_plugin(pkg)
    assert plugin is not None
    assert plugin.meta.name == "hello"


def test_load_plugin_package_with_manifest(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    pkg = d / "manifest_plugin"
    pkg.mkdir(parents=True)
    (pkg / "plugin.py").write_text(SIMPLE_PLUGIN)
    plugin = load_plugin(pkg)
    assert plugin is not None
    assert plugin.meta.name == "hello"


def test_load_plugin_no_class_returns_none(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "empty", "x = 1")
    assert load_plugin(d / "empty.py") is None


def test_load_plugin_import_error_returns_none(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "broken", "import nonexistent_xyz")
    assert load_plugin(d / "broken.py") is None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discover_empty(workspace: Path) -> None:
    found = discover_plugin_paths(workspace, root=workspace / "global")
    assert found == []


def test_discover_finds_py_file(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "my_plugin", SIMPLE_PLUGIN)
    found = discover_plugin_paths(workspace, root=workspace / "global")
    assert len(found) == 1
    assert found[0].name == "my_plugin.py"


def test_discover_skips_underscore(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "_internal", SIMPLE_PLUGIN)
    found = discover_plugin_paths(workspace, root=workspace / "global")
    assert len(found) == 0


def test_discover_skips_dirs_without_entry(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    pkg = d / "bad_dir"
    pkg.mkdir(parents=True)
    (pkg / "not_plugin.py").write_text(SIMPLE_PLUGIN)
    found = discover_plugin_paths(workspace, root=workspace / "global")
    assert len(found) == 0


# ---------------------------------------------------------------------------
# PluginLoader
# ---------------------------------------------------------------------------

def test_loader_with_no_plugins(workspace: Path) -> None:
    loader = PluginLoader(workspace, root=_root(workspace))
    assert loader.plugins == []
    assert loader.errors == []


def test_loader_loads_plugins(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "hello", SIMPLE_PLUGIN)
    loader = PluginLoader(workspace, root=_root(workspace))
    assert len(loader.plugins) == 1
    assert loader.plugins[0].meta.name == "hello"
    assert loader.errors == []
    assert loader.is_loaded("hello")


def test_loader_dependency_validation(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "dependent", """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class DepPlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="dependent", dependencies=("missing",))

    def register(self, api: PluginAPI) -> None:
        pass
""")
    loader = PluginLoader(workspace, root=_root(workspace))
    assert len(loader.errors) == 1
    assert "missing" in loader.errors[0]
    assert len(loader.plugins) == 1  # still loaded
    assert wire_plugins(loader) == []


def test_runtime_dependency_must_be_satisfied(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "dependent", """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata
class DepPlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="dependent")
    def register(self, api: PluginAPI) -> None:
        api.require("missing")
""")

    loader = PluginLoader(workspace, root=_root(workspace))

    assert wire_plugins(loader) == []
    assert "runtime dependencies" in loader.errors[-1]


def test_loader_extra_paths(workspace: Path, tmp_path: Path) -> None:
    extra = tmp_path / "extra_plugin.py"
    extra.write_text(SIMPLE_PLUGIN)
    loader = PluginLoader(workspace, root=_root(workspace), extra_paths=[str(extra)])
    assert loader.is_loaded("hello")


def test_loader_source_of(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "hello", SIMPLE_PLUGIN)
    loader = PluginLoader(workspace, root=_root(workspace))
    src = loader.source_of("hello")
    assert src is not None
    assert "hello.py" in str(src)


# ---------------------------------------------------------------------------
# wire_plugins
# ---------------------------------------------------------------------------

def test_wire_plugins_registers_all(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "tool_plugin", """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata
from evopi.core.tool import Tool

class ToolPlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="tool-plugin")

    def register(self, api: PluginAPI) -> None:
        api.register_tool(Tool(name="add", description="Adds", parameters={}, handler=lambda: 42))
""")
    loader = PluginLoader(workspace, root=_root(workspace))
    apis = wire_plugins(loader)
    assert len(apis) == 1
    assert len(apis[0].tools) == 1
    assert apis[0].tools[0].name == "add"
    assert apis[0].tools[0].metadata.get("plugin_source") == "tool-plugin"


def test_wire_plugins_enabled_filter(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "p1", """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata
class P1(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="p1")
    def register(self, api: PluginAPI) -> None:
        pass
""")
    _write_plugin(d, "p2", """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata
class P2(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="p2")
    def register(self, api: PluginAPI) -> None:
        pass
""")
    loader = PluginLoader(workspace, root=_root(workspace))
    apis = wire_plugins(loader, enabled={"p1"})
    assert len(apis) == 1
    assert apis[0].plugin_name == "p1"


# ---------------------------------------------------------------------------
# PolicyContext tool_plugin_source (integration check)
# ---------------------------------------------------------------------------


def test_policy_context_has_plugin_source_field() -> None:
    from evopi.policy.types import PolicyContext
    from evopi.core.context import AgentContext

    ctx = PolicyContext(
        hook="before_tool_call",
        agent_context=AgentContext(messages=[], tools=[]),
        tool_plugin_source="my-plugin",
    )
    assert ctx.tool_plugin_source == "my-plugin"
    assert ctx.policy_plugin_source is None
