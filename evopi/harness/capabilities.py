"""Immutable snapshots of capabilities assembled by a Harness."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True, kw_only=True)
class ToolCapability:
    name: str
    description: str
    effects: tuple[str, ...]
    source: str
    plugin: str | None
    active: bool


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyCapability:
    name: str
    version: str
    source: str
    digest: str
    artifact_digest: str | None = None
    activation_id: str | None = None
    selection_id: str | None = None
    replaces: str | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class HarnessCapabilities:
    """Public, read-only view of the capabilities visible to an Agent."""

    tool_names: tuple[str, ...] = ()
    active_tool_names: tuple[str, ...] = ()
    tools: tuple[ToolCapability, ...] = ()
    policy_names: tuple[str, ...] = ()
    policies: tuple[PolicyCapability, ...] = ()
    plugin_names: tuple[str, ...] = ()
    command_names: tuple[str, ...] = ()
    memory_enabled: bool = False
    skills_enabled: bool = False
    warnings: tuple[str, ...] = ()


__all__ = ["HarnessCapabilities", "PolicyCapability", "ToolCapability"]
