"""PluginRuntime — loads, validates, and manages all plugins."""

from __future__ import annotations

import logging
from pathlib import Path

from evopi.core.tool import Tool
from evopi.plugins.loader import discover_plugins, load_plugin
from evopi.plugins.protocol import Plugin, PluginAPI

_logger = logging.getLogger(__name__)


class PluginLoadError(RuntimeError):
    """Raised when a plugin cannot be loaded."""


class PluginRuntime:
    """Loads plugins from filesystem, validates dependencies, and exposes
    collected registrations to the harness.
    """

    def __init__(
        self,
        workspace: str | Path,
        root: str | Path | None = None,
        extra_paths: list[str | Path] | None = None,
    ) -> None:
        self._workspace = Path(str(workspace))
        self._plugins: list[Plugin] = []
        self._errors: list[str] = []
        self._tools: list[Tool] = []
        self._event_handlers: list[tuple[str, object]] = []
        self._commands: list[tuple[str, object]] = []

        # Discover + load
        for path in discover_plugins(workspace, root):
            if plugin := load_plugin(path):
                self._plugins.append(plugin)
        for p in (extra_paths or []):
            if plugin := load_plugin(Path(p)):
                self._plugins.append(plugin)

        # Validate dependencies
        names = {p.meta.name for p in self._plugins}
        for plugin in self._plugins:
            for dep in plugin.meta.dependencies:
                if dep not in names:
                    self._errors.append(
                        f"Plugin '{plugin.meta.name}' requires '{dep}' which is not loaded"
                    )

        # Run register() on each plugin
        for plugin in self._plugins:
            try:
                api = PluginAPI(plugin.meta.name)
                plugin.register(api)
                self._tools.extend(api._tools)
                self._event_handlers.extend(api._events)
                self._commands.extend(api._commands)
                # Policies and packs are collected separately by the harness
                self._policies = getattr(self, "_policies", []) + api._policies
                self._packs = getattr(self, "_packs", []) + api._packs
            except Exception:
                _logger.exception("Plugin '%s' register() failed", plugin.meta.name)
                self._errors.append(f"Plugin '{plugin.meta.name}' failed to register")

    # -- public API -----------------------------------------------------------

    @property
    def plugins(self) -> list[Plugin]:
        return list(self._plugins)

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools)

    @property
    def event_handlers(self) -> list[tuple[str, object]]:
        return list(self._event_handlers)

    @property
    def commands(self) -> list[tuple[str, object]]:
        return list(self._commands)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def policies(self) -> list:
        return list(getattr(self, "_policies", []))

    @property
    def packs(self) -> list:
        return list(getattr(self, "_packs", []))

    def is_loaded(self, name: str) -> bool:
        return any(p.meta.name == name for p in self._plugins)


__all__ = ["PluginLoadError", "PluginRuntime"]
