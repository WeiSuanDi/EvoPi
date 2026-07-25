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
    name: str
    version: str = "0.1.0"
    description: str = ""
    source_path: str = ""
    dependencies: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Plugin base class
# ---------------------------------------------------------------------------


class Plugin(ABC):
    """A plugin registers tools, handlers, commands, and policies.

    Each ``.py`` file in ``~/.evopi/plugins/`` or ``<project>/.evopi/plugins/``
    that exports a class inheriting from ``Plugin`` is auto-discovered.
    """

    @property
    @abstractmethod
    def meta(self) -> PluginMetadata: ...

    @abstractmethod
    def register(self, api: PluginAPI) -> None:
        """Called at load time.  Use *api* to declare tools, policies, etc."""
        ...


# ---------------------------------------------------------------------------
# PluginAPI
# ---------------------------------------------------------------------------

EventHandler = Callable[..., Any]  # called with CoreEvent


class PluginAPI:
    """Registration surface passed to :meth:`Plugin.register`.

    All registrations are collected by the :class:`PluginRuntime` and applied
    when the harness is wired.
    """

    def __init__(self, plugin_name: str) -> None:
        self._plugin = plugin_name
        self._tools: list[Tool] = []
        self._events: list[tuple[str, EventHandler]] = []
        self._commands: list[tuple[str, EventHandler]] = []
        self._policies: list[Policy] = []
        self._packs: list[PolicyPack] = []

    # -- tools ----------------------------------------------------------------

    def register_tool(self, tool: Tool) -> Tool:
        """Register a tool the LLM can call.

        The tool's description is appended with the plugin source so
        Policies can identify its origin via tool name matching.
        """
        # Tag the tool as plugin-sourced via the description
        if f"plugin:{self._plugin}" not in tool.description:
            object.__setattr__(
                tool,
                "description",
                tool.description + f" [plugin:{self._plugin}]",
            )
        self._tools.append(tool)
        return tool

    # -- event handlers -------------------------------------------------------

    def on(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe *handler* to a CoreEvent type.

        Valid types: ``"agent_start"``, ``"message_update"``,
        ``"tool_execution_start"``, etc.
        """
        self._events.append((event_type, handler))

    # -- commands -------------------------------------------------------------

    def register_command(self, name: str, handler: EventHandler) -> None:
        """Register a ``/`` command callable from the REPL."""
        self._commands.append((name, handler))

    # -- policies -------------------------------------------------------------

    def register_policy(self, policy: Policy) -> None:
        """Register a Policy that the plugin provides."""
        self._policies.append(policy)

    def load_policy_pack(self, pack: PolicyPack) -> None:
        """Load a complete PolicyPack."""
        self._packs.append(pack)

    # -- dependencies ---------------------------------------------------------

    def require(self, plugin_name: str, version: str | None = None) -> None:
        """Declare a dependency on another plugin."""
        # Dependencies are validated by PluginRuntime after all plugins are loaded.
        pass  # metadata-only for now; checked at runtime


__all__ = ["Plugin", "PluginAPI", "PluginMetadata"]
