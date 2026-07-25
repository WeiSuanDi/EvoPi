"""Tests for the Plugin system."""

import tempfile
from pathlib import Path

from evopi.core.tool import Tool
from evopi.plugins.loader import load_plugin
from evopi.plugins.protocol import Plugin, PluginAPI, PluginMetadata
from evopi.plugins.runtime import PluginRuntime


# ---------------------------------------------------------------------------
# Plugin protocol
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


def test_plugin_registers_tool() -> None:
    plugin = _TestPlugin()
    api = PluginAPI(plugin.meta.name)
    plugin.register(api)
    assert len(api._tools) == 1
    assert api._tools[0].name == "test_tool"
    assert "plugin:test" in api._tools[0].description


def test_plugin_registers_events() -> None:
    plugin = _TestPlugin()
    api = PluginAPI(plugin.meta.name)
    plugin.register(api)
    assert len(api._events) == 1
    assert api._events[0][0] == "agent_start"


def test_plugin_registers_commands() -> None:
    plugin = _TestPlugin()
    api = PluginAPI(plugin.meta.name)
    plugin.register(api)
    assert len(api._commands) == 1
    assert api._commands[0][0] == "/test"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _write_plugin_file(directory: Path, name: str, content: str) -> Path:
    path = directory / f"{name}.py"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_plugin_from_py_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_plugin_file(d, "hello_plugin", """
from evopi.plugins.protocol import Plugin, PluginAPI, PluginMetadata
from evopi.core.tool import Tool

class HelloPlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="hello", version="1.0", description="Hello world")

    def register(self, api: PluginAPI) -> None:
        api.register_tool(Tool(
            name="greet", description="Greet the world",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "hello world",
        ))
""")
        plugin = load_plugin(d / "hello_plugin.py")
        assert plugin is not None
        assert plugin.meta.name == "hello"


def test_load_plugin_invalid_file_returns_none() -> None:
    assert load_plugin("/nonexistent/plugin.py") is None


# ---------------------------------------------------------------------------
# PluginRuntime
# ---------------------------------------------------------------------------

def test_runtime_loads_plugins() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        plugins_dir = d / ".evopi" / "plugins"
        plugins_dir.mkdir(parents=True)
        _write_plugin_file(plugins_dir, "test_rt", """
from evopi.plugins.protocol import Plugin, PluginAPI, PluginMetadata
from evopi.core.tool import Tool

class TestRtPlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="test_rt", version="1.0")

    def register(self, api: PluginAPI) -> None:
        api.register_tool(Tool(
            name="add", description="Add numbers",
            parameters={"type": "object", "properties": {}},
            handler=lambda: 42,
        ))
        api.register_command("/add", lambda t: None)
""")
        runtime = PluginRuntime(workspace=d, root=d)
        assert len(runtime.tools) == 1
        assert runtime.tools[0].name == "add"
        assert len(runtime.commands) == 1
        assert runtime.commands[0][0] == "/add"


def test_runtime_with_no_plugins() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime = PluginRuntime(workspace=tmp, root=tmp)
        assert len(runtime.plugins) == 0
        assert len(runtime.tools) == 0
