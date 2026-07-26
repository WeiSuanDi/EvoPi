"""EvoPi Skills — structured task experience packages.

Skills are Markdown documents with YAML frontmatter, discovered from
``~/.evopi/skills/`` and ``<project>/.evopi/skills/``.  They describe
reusable capability patterns that can be injected into the agent context.
"""

from evopi.skills.loader import discover_skill_paths, load_skill
from evopi.skills.registry import SkillLoader, SkillRegistry
from evopi.skills.types import Skill

__all__ = [
    "Skill",
    "SkillLoader",
    "SkillRegistry",
    "discover_skill_paths",
    "load_skill",
]
