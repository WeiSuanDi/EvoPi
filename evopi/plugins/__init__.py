"""EvoPi Plugin system — Pi-style extensibility for Python agent runtimes.

Plugins are auto-discovered from ``~/.evopi/plugins/`` and
``<project>/.evopi/plugins/``.  Each plugin can register tools, event
handlers, commands, and policies.
"""

from evopi.plugins.loader import PluginLoader, discover_plugin_paths, load_plugin
from evopi.plugins.protocol import EventHandler, Plugin, PluginAPI, PluginMetadata
from evopi.plugins.runtime import filtered_event_listener, wire_plugins
from evopi.plugins.lifecycle import approved_plugin_entrypoints, resolve_evopi_home
from evopi.plugins.candidates import (
    PLUGIN_MANIFEST_SCHEMA_VERSION,
    PluginCandidate,
    PluginCandidateError,
    PluginCandidateStatus,
    PluginArtifactStore,
    PluginManager,
    PluginManifest,
    PluginReviewReport,
    PluginState,
    review_plugin,
)

__all__ = [
    "EventHandler",
    "Plugin",
    "PluginAPI",
    "PluginLoader",
    "PluginMetadata",
    "discover_plugin_paths",
    "load_plugin",
    "wire_plugins",
    "filtered_event_listener",
    "approved_plugin_entrypoints",
    "resolve_evopi_home",
    "PLUGIN_MANIFEST_SCHEMA_VERSION",
    "PluginCandidate",
    "PluginCandidateError",
    "PluginCandidateStatus",
    "PluginArtifactStore",
    "PluginManager",
    "PluginManifest",
    "PluginReviewReport",
    "PluginState",
    "review_plugin",
]
