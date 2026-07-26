"""Packaged Plugin SDK templates and safe candidate scaffolding."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginTemplate:
    name: str
    description: str


_TEMPLATES = {
    "basic": PluginTemplate(
        name="basic",
        description="Minimal PluginAPI v1 command and test.",
    ),
    "plan-mode": PluginTemplate(
        name="plan-mode",
        description="Governed planning workflow built only with PluginAPI v1.",
    ),
}


def available_plugin_templates() -> dict[str, PluginTemplate]:
    """Return the deterministic set shipped in the installed package."""

    return dict(_TEMPLATES)


def plugin_sdk_guide() -> str:
    """Read the PluginAPI guide from installed package data."""

    return (
        files("evopi.plugins")
        .joinpath("sdk_templates", "PLUGIN_API_V1.md")
        .read_text(encoding="utf-8")
    )


def initialize_plugin_candidate(
    name: str,
    *,
    template: str = "basic",
    path: str | Path,
) -> Path:
    """Render one packaged template into a new, inactive candidate directory."""

    normalized = name.strip().lower()
    if not _PLUGIN_NAME.fullmatch(normalized):
        raise ValueError(
            "Plugin name must start with a letter and contain only "
            "lowercase letters, digits, or hyphens"
        )
    if template not in _TEMPLATES:
        raise ValueError(f"Unknown Plugin template: {template}")
    target = Path(path).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"Plugin candidate target is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    class_name = "".join(part.capitalize() for part in normalized.split("-")) + "Plugin"
    replacements = {
        "{{PLUGIN_NAME}}": normalized,
        "{{CLASS_NAME}}": class_name,
    }
    resource_root = files("evopi.plugins").joinpath("sdk_templates", template)
    _render_tree(resource_root, target, replacements)
    return target


def _render_tree(resource: Any, target: Path, replacements: dict[str, str]) -> None:
    for child in resource.iterdir():
        output_name = child.name.removesuffix(".tmpl")
        output = target / output_name
        if child.is_dir():
            output.mkdir(parents=True, exist_ok=True)
            _render_tree(child, output, replacements)
            continue
        content = child.read_text(encoding="utf-8")
        for marker, value in replacements.items():
            content = content.replace(marker, value)
        output.write_text(content, encoding="utf-8")


__all__ = [
    "PluginTemplate",
    "available_plugin_templates",
    "initialize_plugin_candidate",
    "plugin_sdk_guide",
]
