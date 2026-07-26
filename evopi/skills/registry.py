"""Skill registry — storage, lookup, and context injection."""

from __future__ import annotations

from collections.abc import Iterator

from pathlib import Path

from evopi.skills.loader import SkillLoadError, load_skill_strict
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
        max_skill_chars: int = 12_000,
        max_total_chars: int = 24_000,
    ) -> None:
        self._registry = SkillRegistry()
        self._errors: list[str] = []
        self._max_skill_chars = max_skill_chars
        self._max_total_chars = max_total_chars
        self._total_chars = 0
        paths: list[Path] = []
        if root is not None:
            skill_root = Path(root).expanduser().resolve()
            if skill_root.is_dir():
                paths.extend(_scan_skill_root(skill_root))
            else:
                self._errors.append(f"Skill root is not a directory: {skill_root}")
        for ep in extra_paths or []:
            paths.append(Path(ep).expanduser().resolve())
        for path in paths:
            self._load(path)

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(self._errors)

    def _load(self, path: Path) -> None:
        try:
            skill = load_skill_strict(path)
            size = len(skill.prompt_segment())
            if size > self._max_skill_chars:
                raise SkillLoadError(
                    f"Skill '{skill.name}' exceeds {self._max_skill_chars} characters"
                )
            if self._total_chars + size > self._max_total_chars:
                raise SkillLoadError(
                    f"Skill total injection budget exceeds {self._max_total_chars} characters"
                )
            self._registry.register(skill)
            self._total_chars += size
        except (SkillLoadError, ValueError) as exc:
            self._errors.append(str(exc))


def _scan_skill_root(root: Path) -> list[Path]:
    paths: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".md":
            paths.append(entry)
        elif entry.is_dir() and (entry / "SKILL.md").is_file():
            paths.append(entry / "SKILL.md")
    return paths


__all__ = ["SkillLoader", "SkillRegistry"]
