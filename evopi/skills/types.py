"""Skill type definitions — structured task experience packages.

A Skill is a Markdown document with YAML frontmatter describing a reusable
capability pattern.  Skills are loaded from the filesystem, registered, and
injected into the agent context when relevant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, kw_only=True)
class Skill:
    """A reusable task pattern with instructions and metadata."""

    name: str
    description: str
    content: str  # Markdown body (the instructions the model follows)
    source_path: str = ""
    version: str = "0.1.0"
    tools: list[str] = field(default_factory=list)
    risk_level: str = "low"  # low | medium | high
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches_tool(self, tool_name: str) -> bool:
        """Return True if this skill uses the named tool."""
        return tool_name in self.tools

    def prompt_segment(self) -> str:
        """Render the skill as a context snippet for the model."""
        return f"## Skill: {self.name}\n{self.content}"


__all__ = ["Skill"]
