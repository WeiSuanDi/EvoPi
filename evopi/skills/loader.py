"""Skill file-system discovery and Markdown-with-frontmatter loading.

Mirrors the Agent Skills standard: ``.md`` files with optional YAML
frontmatter, discovered from ``~/.evopi/skills/`` (global) and
``<project>/.evopi/skills/`` (local).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from evopi.skills.types import Skill

_logger = logging.getLogger(__name__)

_SKILL_DIR_NAME = "skills"

# Matches YAML frontmatter between --- delimiters
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
VALID_SKILL_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})


class SkillLoadError(RuntimeError):
    """Raised when a Skill document is malformed or unsafe to inject."""


def discover_skill_paths(
    workspace: str | Path,
    root: str | Path | None = None,
) -> list[Path]:
    """Return absolute paths to all discovered ``.md`` skill files.

    Discovery order (local wins on name collision):
    1. ``<workspace>/.evopi/skills/`` — project-local
    2. ``~/.evopi/skills/`` — global
    """
    discovered: list[Path] = []
    seen: set[str] = set()
    ws_path = Path(workspace).expanduser().resolve()

    local_dir = ws_path / ".evopi" / _SKILL_DIR_NAME
    for p in _scan_dir(local_dir):
        key = p.name
        if key not in seen:
            seen.add(key)
            discovered.append(p)

    global_dir = (
        Path(root).expanduser().resolve()
        if root
        else Path.home() / ".evopi" / _SKILL_DIR_NAME
    )
    for p in _scan_dir(global_dir):
        key = p.name
        if key not in seen:
            seen.add(key)
            discovered.append(p)

    return discovered


def _scan_dir(directory: Path) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    result: list[Path] = []
    try:
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.suffix == ".md" and not entry.name.startswith("_"):
                result.append(entry)
            elif entry.is_dir() and not entry.name.startswith("_"):
                skmd = entry / "SKILL.md"
                if skmd.exists():
                    result.append(skmd)
    except OSError:
        pass
    return result


def load_skill(path: str | Path) -> Skill | None:
    """Parse a ``.md`` file into a :class:`Skill`.

    Supports YAML frontmatter for metadata.  Without frontmatter, the first
    ``# Heading`` is used as the skill name and the first paragraph as the
    description.
    """
    try:
        return load_skill_strict(path)
    except SkillLoadError as exc:
        _logger.warning("%s", exc)
        return None


def load_skill_strict(path: str | Path) -> Skill:
    """Parse one Skill and preserve a structured failure reason."""

    resolved = Path(path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        raise SkillLoadError(f"Skill path does not exist: {resolved}")

    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillLoadError(f"Failed to read Skill {resolved}: {exc}") from exc
    if text.startswith("---") and _FRONTMATTER_RE.match(text) is None:
        raise SkillLoadError(f"Skill frontmatter is not closed: {resolved}")

    frontmatter: dict[str, str] = {}
    body = text

    match = _FRONTMATTER_RE.match(text)
    if match:
        frontmatter = _parse_frontmatter(match.group(1))
        body = text[match.end():]

    name = frontmatter.get("name") or _extract_name(body) or resolved.stem
    description = frontmatter.get("description") or _extract_description(body) or ""
    version = frontmatter.get("version", "0.1.0")
    risk_level = frontmatter.get("risk_level", "low")
    if risk_level not in VALID_SKILL_RISK_LEVELS:
        raise SkillLoadError(
            f"Skill '{name}' has invalid risk_level '{risk_level}'"
        )
    tools_raw = frontmatter.get("tools", "")

    return Skill(
        name=name,
        description=description,
        content=body.strip(),
        source_path=str(resolved),
        version=version,
        tools=[t.strip() for t in tools_raw.split(",") if t.strip()],
        risk_level=risk_level,
    )


def _parse_frontmatter(raw: str) -> dict[str, str]:
    """Minimal YAML parser for flat key: value pairs — avoids pyyaml dependency."""
    result: dict[str, str] = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip("\"'")
            result[key] = value
    return result


def _extract_name(body: str) -> str | None:
    """Extract the first # heading as the skill name."""
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def _extract_description(body: str) -> str | None:
    """Extract the first non-empty, non-heading paragraph."""
    lines = body.split("\n")
    in_heading = True
    for line in lines:
        stripped = line.strip()
        if not stripped:
            in_heading = False
            continue
        if in_heading and stripped.startswith("#"):
            continue
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


__all__ = [
    "SkillLoadError",
    "VALID_SKILL_RISK_LEVELS",
    "discover_skill_paths",
    "load_skill",
    "load_skill_strict",
]
