"""Convenience helpers for wiring plugins into a harness.

The heavy lifting (discovery, loading, dependency validation) lives in
:mod:`evopi.plugins.loader`.  This module provides thin orchestration
utilities that call ``register()`` and collect registrations.
"""

from __future__ import annotations


from evopi.core.tool import Tool
from evopi.plugins.loader import PluginLoader, discover_plugin_paths, load_plugin
from evopi.plugins.protocol import Plugin, PluginAPI
from evopi.policy.registry import PolicyPack
from evopi.policy.types import Policy

# Re-export for convenience
__all__ = [
    "Plugin",
    "PluginAPI",
    "PluginLoader",
    "Policy",
    "PolicyPack",
    "Tool",
    "discover_plugin_paths",
    "load_plugin",
    "wire_plugins",
]


def wire_plugins(
    loader: PluginLoader,
    *,
    enabled: set[str] | None = None,
) -> list[PluginAPI]:
    """Call ``register()`` on every plugin in *loader*, return the filled APIs.

    This is a convenience for harnesses that want to call ``register()`` in a
    standard order and then wire the results themselves.

    *enabled* limits registration to a whitelist of plugin names; when *None*,
    all loaded plugins are registered.
    """
    apis: list[PluginAPI] = []
    for plugin in loader.plugins:
        name = plugin.meta.name
        if enabled is not None and name not in enabled:
            continue
        api = PluginAPI(name, plugin.meta.version)
        try:
            plugin.register(api)
        except Exception:
            loader.add_error(f"Plugin '{name}' register() raised an exception")
            continue
        apis.append(api)
    return apis
