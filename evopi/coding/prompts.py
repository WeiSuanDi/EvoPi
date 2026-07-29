"""Minimal dynamic Coding prompt assembled from active capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evopi.core.tool import Tool

BASE_SYSTEM_PROMPT = """\
You are an expert coding assistant operating inside EvoPi.
Work carefully, make small verifiable changes, and report only outcomes confirmed by tools.

## Governance

Policy may block, require confirmation, rewrite arguments, or trigger validation.
Never bypass Policy, fabricate approval, or claim that reloading approves a candidate.

## Extension boundary

Only when the user explicitly asks to extend EvoPi, use the packaged candidate SDK and
the formal candidate → review → approval → reload lifecycle.
"""


def build_system_prompt(
    tools: list[Tool],
    *,
    base: str = BASE_SYSTEM_PROMPT,
    workspace: str | Path | None = None,
    append: str | None = None,
) -> str:
    """Build a concise prompt from the final active Tool view."""

    ordered = sorted(tools, key=lambda item: item.name)
    lines = [base.rstrip(), "", "## Available Tools", ""]
    if not ordered:
        lines.append("Available tools: none.")
    else:
        for tool in ordered:
            snippet = tool.metadata.get("prompt_snippet")
            description = (
                snippet.strip()
                if isinstance(snippet, str) and snippet.strip()
                else tool.description.splitlines()[0].strip()
            )
            plugin = tool.metadata.get("plugin_source")
            source = f" [plugin: {plugin}]" if isinstance(plugin, str) else ""
            lines.append(f"- `{tool.name}` — {description}{source}")

    guidelines = _guidelines(ordered)
    if guidelines:
        lines.extend(("", "## Working Guidelines", ""))
        lines.extend(f"- {guideline}" for guideline in guidelines)
    if workspace is not None:
        lines.extend(
            (
                "",
                "## Workspace",
                "",
                str(Path(workspace).expanduser().resolve()),
            )
        )
    prompt = "\n".join(lines).rstrip()
    if append is not None and append.strip():
        prompt = f"{prompt}\n\n{append.strip()}"
    return prompt


def _guidelines(tools: list[Tool]) -> tuple[str, ...]:
    effects = {
        effect
        for tool in tools
        for effect in _tool_effects(tool.metadata.get("effects"))
    }
    values: list[str] = [
        "Read relevant context before acting and keep changes narrowly scoped.",
        "Use workspace-relative paths and trust Tool results over assumptions.",
    ]
    if "write" in effects:
        values.append("Use exact incremental edits and verify writes before claiming success.")
    if "execute" in effects:
        values.append("Run the smallest relevant verification command and report failures honestly.")
    if "network" in effects:
        values.append("Use network access only when the task requires current external information.")
    if "delegate" in effects:
        values.append("Delegate only bounded tasks and validate child results before relying on them.")
    for tool in tools:
        raw = tool.metadata.get("prompt_guidelines", ())
        candidates: tuple[str, ...]
        if isinstance(raw, str):
            candidates = (raw,)
        elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            candidates = tuple(raw)
        else:
            candidates = ()
        values.extend(item.strip() for item in candidates if item.strip())
    return tuple(dict.fromkeys(values))


def _tool_effects(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return ("unknown",)
    return tuple(item.strip() for item in value if item.strip()) or ("unknown",)


CODING_SYSTEM_PROMPT = BASE_SYSTEM_PROMPT

__all__ = ["BASE_SYSTEM_PROMPT", "CODING_SYSTEM_PROMPT", "build_system_prompt"]
