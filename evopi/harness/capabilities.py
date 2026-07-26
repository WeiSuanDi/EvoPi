"""Immutable snapshots of capabilities assembled by a Harness."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True, kw_only=True)
class HarnessCapabilities:
    """Public, read-only view of the capabilities visible to an Agent."""

    tool_names: tuple[str, ...] = ()
    policy_names: tuple[str, ...] = ()
    plugin_names: tuple[str, ...] = ()
    command_names: tuple[str, ...] = ()
    memory_enabled: bool = False
    skills_enabled: bool = False
    warnings: tuple[str, ...] = ()


__all__ = ["HarnessCapabilities"]
