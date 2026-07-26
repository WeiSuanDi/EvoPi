"""Skill registry — storage, lookup, and context injection."""

from __future__ import annotations

from collections.abc import Iterator

from evopi.skills.loader import discover_skill_paths, load_skill
from evopi.skills.types import Skill


class SkillRegistry:
    """Named collection of loaded skills with relevance-based lookup."""

    def __init__(self, skills: list[Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        if skills:
            for skill in skills:
                self.register(skill)

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"Skill '{skill.name}' is already registered")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def search(self, query: str, *, limit: int = 5) -> list[Skill]:
        """Keyword search across skill names and descriptions."""
        words = [w.lower() for w in query.split() if len(w) >= 2]
        if not words:
            return list(self._skills.values())[:limit]
        results: list[Skill] = []
        for skill in self._skills.values():
            text = (skill.name + " " + skill.description).lower()
            if any(w in text for w in words):
                results.append(skill)
        return results[:limit]

    def for_tools(self, tool_names: set[str]) -> list[Skill]:
        """Return skills that reference any of the given tool names."""
        return [s for s in self._skills.values() if any(s.matches_tool(t) for t in tool_names)]

    def all(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.name)

    def __len__(self) -> int:
        return len(self._skills)

    def __iter__(self) -> Iterator[Skill]:
        return iter(self._skills.values())


class SkillLoader:
    """Discover and load skills from the filesystem into a registry."""

    def __init__(
        self,
        workspace: str,
        root: str | None = None,
        extra_paths: list[str] | None = None,
    ) -> None:
        self._registry = SkillRegistry()
        for path in discover_skill_paths(workspace, root):
            if skill := load_skill(path):
                try:
                    self._registry.register(skill)
                except ValueError:
                    pass  # duplicate name, keep first
        for ep in (extra_paths or []):
            if skill := load_skill(ep):
                try:
                    self._registry.register(skill)
                except ValueError:
                    pass

    @property
    def registry(self) -> SkillRegistry:
        return self._registry


__all__ = ["SkillLoader", "SkillRegistry"]
