"""Packaged scaffold for inactive Policy candidate directories."""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path
from typing import Any

_POLICY_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


def initialize_policy_candidate(name: str, *, path: str | Path) -> Path:
    normalized = name.strip().lower().replace("-", "_")
    if not _POLICY_NAME.fullmatch(normalized):
        raise ValueError(
            "Policy name must start with a letter and contain only "
            "lowercase letters, digits, underscores, or hyphens"
        )
    target = Path(path).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"Policy candidate target is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    class_name = "".join(part.capitalize() for part in normalized.split("_")) + "Policy"
    replacements = {
        "{{POLICY_NAME}}": normalized,
        "{{CLASS_NAME}}": class_name,
    }
    resource_root = files("evopi.evolution").joinpath(
        "policy_templates",
        "basic",
    )
    _render_tree(resource_root, target, replacements)
    return target


def _render_tree(resource: Any, target: Path, replacements: dict[str, str]) -> None:
    for child in resource.iterdir():
        output = target / child.name.removesuffix(".tmpl")
        if child.is_dir():
            output.mkdir(parents=True, exist_ok=True)
            _render_tree(child, output, replacements)
            continue
        content = child.read_text(encoding="utf-8")
        for marker, value in replacements.items():
            content = content.replace(marker, value)
        output.write_text(content, encoding="utf-8")


__all__ = ["initialize_policy_candidate"]
