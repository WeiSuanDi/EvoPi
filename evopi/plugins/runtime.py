"""Convenience helpers for wiring plugins into a harness.

The heavy lifting (discovery, loading, dependency validation) lives in
:mod:`evopi.plugins.loader`.  This module provides thin orchestration
utilities that call ``register()`` and collect registrations.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from evopi.core.tool import Tool
from evopi.core.events import CoreEvent, EventListener
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
    "filtered_event_listener",
    "wire_plugins",
]


def filtered_event_listener(
    event_type: str,
    handler: Callable[..., Awaitable[None] | None],
    *,
    plugin_name: str = "unknown",
    on_contract_error: Callable[[str], None] | None = None,
) -> EventListener:
    """Wrap a Plugin handler so it receives only its declared event type."""

    def listener(event: CoreEvent) -> Awaitable[None] | None:
        if event.type != event_type:
            return None
        try:
            result = handler(event)
        except Exception as exc:
            _report_contract_error(
                on_contract_error,
                f"Plugin '{plugin_name}' event handler for '{event_type}' raised "
                f"{type(exc).__name__}: {exc}",
            )
            return None
        if inspect.isawaitable(result):
            return _finish_event_handler(
                result,
                plugin_name=plugin_name,
                event_type=event_type,
                on_contract_error=on_contract_error,
            )
        if result is not None:
            _report_observational_return(
                plugin_name,
                event_type,
                on_contract_error,
            )
        return None

    return listener


async def _finish_event_handler(
    result: Awaitable[object],
    *,
    plugin_name: str,
    event_type: str,
    on_contract_error: Callable[[str], None] | None,
) -> None:
    try:
        value = await result
    except Exception as exc:
        _report_contract_error(
            on_contract_error,
            f"Plugin '{plugin_name}' event handler for '{event_type}' raised "
            f"{type(exc).__name__}: {exc}",
        )
        return
    if value is not None:
        _report_observational_return(
            plugin_name,
            event_type,
            on_contract_error,
        )


def _report_observational_return(
    plugin_name: str,
    event_type: str,
    callback: Callable[[str], None] | None,
) -> None:
    _report_contract_error(
        callback,
        f"Plugin '{plugin_name}' event handler for '{event_type}' returned a value; "
        "event handlers are observational and Policy is the only execution arbiter",
    )


def _report_contract_error(
    callback: Callable[[str], None] | None,
    message: str,
) -> None:
    if callback is not None:
        callback(message)


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
        if not loader.is_valid(name):
            continue
        api = PluginAPI(name, plugin.meta.version)
        try:
            plugin.register(api)
        except Exception:
            loader.add_error(f"Plugin '{name}' register() raised an exception")
            continue
        apis.append(api)

    versions = {api.plugin_name: api.plugin_version for api in apis}
    valid: list[PluginAPI] = []
    for api in apis:
        missing = [
            dependency
            for dependency, requirement in api._declared_deps
            if dependency not in versions
            or not _version_matches(versions[dependency], requirement)
        ]
        if missing:
            loader.add_error(
                f"Plugin '{api.plugin_name}' has unsatisfied runtime "
                f"dependencies: {', '.join(missing)}"
            )
            continue
        valid.append(api)
    return valid


def _version_matches(version: str, requirement: str | None) -> bool:
    if requirement is None or not requirement.strip():
        return True
    requirement = requirement.strip()
    if requirement.startswith(">="):
        return _version_key(version) >= _version_key(requirement[2:])
    if requirement.startswith("=="):
        requirement = requirement[2:]
    return version == requirement


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(
        int(part) if part.isdigit() else 0
        for part in value.replace("-", ".").split(".")
    )
