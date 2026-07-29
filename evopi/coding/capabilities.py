"""Read-only CodingHarness resource snapshots."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True, kw_only=True)
class MemoryResourceCapability:
    enabled: bool
    entry_count: int


@dataclass(slots=True, frozen=True, kw_only=True)
class SkillResourceCapability:
    name: str
    version: str
    risk_level: str
    source: str


@dataclass(slots=True, frozen=True, kw_only=True)
class CodingResources:
    memory: MemoryResourceCapability
    skills: tuple[SkillResourceCapability, ...]
    subagent_enabled: bool


__all__ = [
    "CodingResources",
    "MemoryResourceCapability",
    "SkillResourceCapability",
]
