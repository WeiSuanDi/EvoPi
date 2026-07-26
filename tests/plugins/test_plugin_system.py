"""Extended edge-case tests for the Plugin system v2."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from evopi.plugins import (
    PluginLoader,
    discover_plugin_paths,
    load_plugin,
    wire_plugins,
)


@pytest.fixture
def workspace() -> Path:
    """Isolated workspace with its own global root."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "workspace"
        ws.mkdir()
        yield ws


def _root(ws: Path) -> Path:
    return ws / "global_root"


def _write_plugin(plugins_dir: Path, name: str, body: str) -> Path:
    plugins_dir.mkdir(parents=True, exist_ok=True)
    path = plugins_dir / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


SIMPLE = """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class SimplePlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="simple", version="1.0")

    def register(self, api: PluginAPI) -> None:
        pass
"""


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_discover_deduplicates_by_name(workspace: Path) -> None:
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "my_plugin", SIMPLE)
    global_dir = _root(workspace) / "plugins"
    global_dir.mkdir(parents=True)
    _write_plugin(global_dir, "my_plugin", SIMPLE)  # same file name → dedup
    found = discover_plugin_paths(workspace, root=_root(workspace))
    # Only the local one is kept
    assert len(found) == 1


# ---------------------------------------------------------------------------
# Plugin instantiation edge cases
# ---------------------------------------------------------------------------

def test_plugin_without_meta_override_returns_none(workspace: Path) -> None:
    """A Plugin subclass that doesn't override the abstract meta property
    should fail to instantiate and return None."""
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "bad_meta", """
from evopi.plugins import Plugin, PluginAPI

class BadPlugin(Plugin):
    def register(self, api: PluginAPI) -> None:
        pass
""")
    plugin = load_plugin(d / "bad_meta.py")
    assert plugin is None


def test_plugin_with_register_error_captured(workspace: Path) -> None:
    """wire_plugins captures register() errors and adds them to loader.errors."""
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "bad_register", """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class BadRegPlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="bad-reg")

    def register(self, api: PluginAPI) -> None:
        raise RuntimeError("boom")
""")
    loader = PluginLoader(workspace, root=_root(workspace))
    apis = wire_plugins(loader)
    # The bad plugin fails to register, but other plugins still succeed
    assert len(apis) == 0
    assert any("bad-reg" in e for e in loader.errors)


# ---------------------------------------------------------------------------
# Tool metadata is preserved through wire_plugins
# ---------------------------------------------------------------------------

def test_tool_metadata_preserved(workspace: Path) -> None:

    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "meta_plugin", """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata
from evopi.core.tool import Tool

class MetaPlugin(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="meta-plugin", version="2.5")

    def register(self, api: PluginAPI) -> None:
        t = Tool(name="data-tool", description="x", parameters={}, handler=lambda: 1)
        api.register_tool(t)
""")
    loader = PluginLoader(workspace, root=_root(workspace))
    apis = wire_plugins(loader)
    tool = apis[0].tools[0]
    assert tool.metadata["plugin_source"] == "meta-plugin"
    assert tool.metadata["plugin_version"] == "2.5"
    assert tool.name == "data-tool"
    assert "x" in tool.description  # description untouched


# ---------------------------------------------------------------------------
# PluginLoader errors
# ---------------------------------------------------------------------------

def test_loader_duplicate_name_error(workspace: Path) -> None:
    """Two plugins with the same meta.name → first kept, second recorded as error."""
    d = workspace / ".evopi" / "plugins"
    _write_plugin(d, "a_plugin", """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class A(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="same-name")
    def register(self, api: PluginAPI) -> None:
        pass
""")
    _write_plugin(d, "b_plugin", """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata

class B(Plugin):
    @property
    def meta(self) -> PluginMetadata:
        return PluginMetadata(name="same-name")
    def register(self, api: PluginAPI) -> None:
        pass
""")
    loader = PluginLoader(workspace, root=_root(workspace))
    assert len(loader.plugins) == 1
    assert any("Duplicate" in e for e in loader.errors)
