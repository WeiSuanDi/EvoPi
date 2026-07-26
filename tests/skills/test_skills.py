"""Tests for the Skills module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from evopi.skills import (
    Skill,
    SkillLoader,
    SkillRegistry,
    discover_skill_paths,
    load_skill,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SKILL_WITH_FM = """---
name: pytest-guide
description: How to write and run pytest tests
version: "1.0"
tools: shell_command
risk_level: low
---

# Pytest Guide

Always run `python -m pytest -q`.

## Steps
1. Write test
2. Run pytest
3. Fix failures
"""

SKILL_NO_FM = """# Simple Skill

Just do the thing properly.
"""


@pytest.fixture
def workspace() -> Path:
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "workspace"
        ws.mkdir()
        yield ws


def _make_skill_dir(ws: Path) -> Path:
    d = ws / ".evopi" / "skills"
    d.mkdir(parents=True)
    return d


def _root(ws: Path) -> Path:
    return ws / "global_root"


# ---------------------------------------------------------------------------
# Skill type
# ---------------------------------------------------------------------------

def test_skill_prompt_segment() -> None:
    s = Skill(name="test", description="d", content="do the thing")
    segment = s.prompt_segment()
    assert "## Skill: test" in segment
    assert "do the thing" in segment


def test_skill_matches_tool() -> None:
    s = Skill(name="t", description="d", content="c", tools=["shell_command"])
    assert s.matches_tool("shell_command")
    assert not s.matches_tool("write_file")


# ---------------------------------------------------------------------------
# Loader — frontmatter parsing
# ---------------------------------------------------------------------------

def test_load_with_frontmatter(workspace: Path) -> None:
    d = _make_skill_dir(workspace)
    (d / "pytest.md").write_text(SKILL_WITH_FM)
    skill = load_skill(d / "pytest.md")
    assert skill is not None
    assert skill.name == "pytest-guide"
    assert skill.description == "How to write and run pytest tests"
    assert skill.version == "1.0"
    assert skill.tools == ["shell_command"]
    assert skill.risk_level == "low"
    assert "pytest" in skill.content
    assert "pytest.md" in skill.source_path


def test_load_without_frontmatter(workspace: Path) -> None:
    d = _make_skill_dir(workspace)
    (d / "simple.md").write_text(SKILL_NO_FM)
    skill = load_skill(d / "simple.md")
    assert skill is not None
    assert skill.name == "Simple Skill"
    assert skill.description == "Just do the thing properly."


def test_load_nonexistent() -> None:
    assert load_skill("/nonexistent/skill.md") is None


# ---------------------------------------------------------------------------
# Loader — SKILL.md in subdirectory
# ---------------------------------------------------------------------------

def test_discover_skill_in_subdirectory(workspace: Path) -> None:
    d = _make_skill_dir(workspace)
    sub = d / "my-skill"
    sub.mkdir()
    (sub / "SKILL.md").write_text(SKILL_WITH_FM)
    paths = discover_skill_paths(workspace, root=_root(workspace))
    assert len(paths) == 1
    assert paths[0].name == "SKILL.md"


def test_discover_skips_underscore_prefix(workspace: Path) -> None:
    d = _make_skill_dir(workspace)
    (d / "_internal.md").write_text(SKILL_NO_FM)
    paths = discover_skill_paths(workspace, root=_root(workspace))
    assert len(paths) == 0


def test_discover_empty_workspace(workspace: Path) -> None:
    paths = discover_skill_paths(workspace, root=_root(workspace))
    assert paths == []


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------

def test_registry_register_and_get() -> None:
    reg = SkillRegistry()
    s = Skill(name="test", description="d", content="c")
    reg.register(s)
    assert reg.get("test") is s
    assert reg.get("nonexistent") is None


def test_registry_duplicate_raises() -> None:
    reg = SkillRegistry()
    reg.register(Skill(name="dup", description="d", content="c"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(Skill(name="dup", description="d2", content="c2"))


def test_registry_search_keyword() -> None:
    reg = SkillRegistry()
    reg.register(Skill(name="pytest", description="Testing with pytest", content="..."))
    reg.register(Skill(name="git", description="Version control", content="..."))
    results = reg.search("testing framework")
    assert len(results) == 1
    assert results[0].name == "pytest"


def test_registry_for_tools() -> None:
    reg = SkillRegistry()
    reg.register(Skill(name="t1", description="d", content="c", tools=["shell_command"]))
    reg.register(Skill(name="t2", description="d", content="c", tools=["write_file"]))
    matches = reg.for_tools({"shell_command"})
    assert len(matches) == 1
    assert matches[0].name == "t1"


def test_registry_all_sorted() -> None:
    reg = SkillRegistry()
    reg.register(Skill(name="b", description="d", content="c"))
    reg.register(Skill(name="a", description="d", content="c"))
    names = [s.name for s in reg.all()]
    assert names == ["a", "b"]


def test_registry_length_and_iter() -> None:
    reg = SkillRegistry()
    reg.register(Skill(name="a", description="d", content="c"))
    reg.register(Skill(name="b", description="d", content="c"))
    assert len(reg) == 2
    assert {s.name for s in reg} == {"a", "b"}


# ---------------------------------------------------------------------------
# SkillLoader
# ---------------------------------------------------------------------------

def test_loader_empty(workspace: Path) -> None:
    loader = SkillLoader(workspace=str(workspace), root=str(_root(workspace)))
    assert len(loader.registry) == 0


def test_loader_discovers_and_loads(workspace: Path) -> None:
    d = _make_skill_dir(workspace)
    (d / "pytest.md").write_text(SKILL_WITH_FM)
    loader = SkillLoader(workspace=str(workspace), root=str(d))
    assert len(loader.registry) == 1
    assert loader.registry.get("pytest-guide") is not None


def test_loader_extra_paths(workspace: Path, tmp_path: Path) -> None:
    extra = tmp_path / "extra.md"
    extra.write_text(SKILL_WITH_FM)
    loader = SkillLoader(
        workspace=str(workspace),
        root=str(_root(workspace)),
        extra_paths=[str(extra)],
    )
    assert len(loader.registry) == 1
    assert loader.registry.get("pytest-guide") is not None


def test_loader_root_is_the_actual_skills_directory(
    workspace: Path,
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "custom-skills"
    skills_dir.mkdir()
    (skills_dir / "pytest.md").write_text(SKILL_WITH_FM, encoding="utf-8")

    loader = SkillLoader(workspace=str(workspace), root=str(skills_dir))

    assert loader.registry.get("pytest-guide") is not None


def test_loader_reports_duplicate_and_invalid_risk(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "a.md").write_text(SKILL_WITH_FM, encoding="utf-8")
    (skills_dir / "b.md").write_text(SKILL_WITH_FM, encoding="utf-8")
    (skills_dir / "bad.md").write_text(
        SKILL_WITH_FM.replace("risk_level: low", "risk_level: impossible"),
        encoding="utf-8",
    )

    loader = SkillLoader(workspace=str(tmp_path), root=str(skills_dir))

    assert len(loader.registry) == 1
    assert any("already registered" in error for error in loader.errors)
    assert any("invalid risk_level" in error for error in loader.errors)


def test_loader_enforces_per_skill_injection_limit(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "large.md").write_text(
        "# Large\n\n" + ("x" * 200),
        encoding="utf-8",
    )

    loader = SkillLoader(
        workspace=str(tmp_path),
        root=str(skills_dir),
        max_skill_chars=100,
    )

    assert len(loader.registry) == 0
    assert "exceeds 100" in loader.errors[0]


# ---------------------------------------------------------------------------
# Skill context rendering
# ---------------------------------------------------------------------------

def test_skill_render_includes_name_and_content() -> None:
    s = Skill(name="git-workflow", description="d", content="Use `git status` first.")
    rendered = s.prompt_segment()
    assert "## Skill: git-workflow" in rendered
    assert "git status" in rendered
