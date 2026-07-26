"""Plugin protocol, PluginAPI, and metadata."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from evopi.core.tool import Tool
from evopi.policy.registry import PolicyPack
from evopi.policy.types import Policy

# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginMetadata:
    """Immutable identity card for a Plugin.

    Declared by the plugin author.  The loader validates *dependencies* against
    other discovered plugins before registration begins.
    """

    name: str
    version: str = "0.1.0"
    description: str = ""
    dependencies: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Plugin base class
# ---------------------------------------------------------------------------


class Plugin(ABC):
    """A plugin registers tools, handlers, commands, and policies.

    Each ``.py`` file in ``~/.evopi/plugins/`` or ``<project>/.evopi/plugins/``
    that exports a class inheriting from ``Plugin`` is auto-discovered by
    :class:`PluginLoader`.
    """

    @property
    @abstractmethod
    def meta(self) -> PluginMetadata:
        """Return the plugin's identity metadata (called once by the loader)."""
        ...

    @abstractmethod
    def register(self, api: PluginAPI) -> None:
        """Called by the harness after discovery.

        Use *api* to declare tools, event handlers, commands, and policies.
        """
        ...


# ---------------------------------------------------------------------------
# PluginAPI — registration surface
# ---------------------------------------------------------------------------

EventHandler = Callable[..., Any]


class PluginAPI:
    """Registration surface passed to :meth:`Plugin.register`.

    All registrations are collected by the harness and wired into the
    appropriate registries (ToolRegistry, PolicyRegistry, EventBus, etc.).

    Tools are automatically tagged with ``metadata["plugin_source"]`` and
    ``metadata["plugin_version"]`` so that Policies can identify their origin.
    """

    def __init__(self, plugin_name: str, plugin_version: str) -> None:
        self.plugin_name = plugin_name
        self.plugin_version = plugin_version

        # -- collected registrations (public, read by harness) --
        self.tools: list[Tool] = []
        self.events: list[tuple[str, EventHandler]] = []
        self.commands: list[tuple[str, EventHandler]] = []
        self.policies: list[Policy] = []
        self.policy_packs: list[PolicyPack] = []

        # -- runtime dependencies declared via require() --
        self._declared_deps: list[tuple[str, str | None]] = []

    # -- tools ----------------------------------------------------------------

    def register_tool(self, tool: Tool) -> Tool:
        """Register a tool the LLM can call.

        The tool's ``metadata`` dict is tagged with ``plugin_source`` and
        ``plugin_version`` so Policies can make plugin-level trust decisions.
        """
        tool.metadata.update(
            plugin_source=self.plugin_name,
            plugin_version=self.plugin_version,
        )
        self.tools.append(tool)
        return tool

    # -- event handlers -------------------------------------------------------

    def on(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe *handler* to a CoreEvent type.

        Valid event types include ``"agent_start"``, ``"message_update"``,
        ``"tool_execution_start"``, ``"error"``, etc.
        """
        self.events.append((event_type, handler))

    # -- commands -------------------------------------------------------------

    def register_command(self, name: str, handler: EventHandler) -> None:
        """Register a ``/`` command callable from a REPL or CLI."""
        self.commands.append((name, handler))

    # -- policies -------------------------------------------------------------

    def register_policy(self, policy: Policy) -> None:
        """Register a Policy that this plugin provides."""
        self.policies.append(policy)

    def load_policy_pack(self, pack: PolicyPack) -> None:
        """Register a complete PolicyPack."""
        self.policy_packs.append(pack)

    # -- dependencies ---------------------------------------------------------

    def require(self, plugin_name: str, version: str | None = None) -> None:
        """Declare a runtime dependency on another plugin.

        Dependencies declared via *require* are validated by the harness after
        all plugins have registered. Static dependencies should be listed in
        :attr:`PluginMetadata.dependencies` instead.
        """
        self._declared_deps.append((plugin_name, version))


__all__ = ["Plugin", "PluginAPI", "PluginMetadata", "EventHandler"]
