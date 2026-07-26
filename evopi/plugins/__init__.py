"""EvoPi Plugin system — Pi-style extensibility for Python agent runtimes.

Plugins are auto-discovered from ``~/.evopi/plugins/`` and
``<project>/.evopi/plugins/``.  Each plugin can register tools, event
handlers, commands, and policies.
"""

from evopi.plugins.loader import PluginLoader, discover_plugin_paths, load_plugin
from evopi.plugins.protocol import EventHandler, Plugin, PluginAPI, PluginMetadata
from evopi.plugins.runtime import wire_plugins

__all__ = [
    "EventHandler",
    "Plugin",
    "PluginAPI",
    "PluginLoader",
    "PluginMetadata",
    "discover_plugin_paths",
    "load_plugin",
    "wire_plugins",
]
