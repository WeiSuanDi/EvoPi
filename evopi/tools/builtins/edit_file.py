"""Exact UTF-8 text replacement with atomic commit."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from evopi.core.tool import Tool, ToolResult
from evopi.tools.schema import object_schema


def create_edit_file_tool(workspace: str | Path) -> Tool:
    root = Path(workspace).resolve()

    def edit_file(path: str, old_text: str, new_text: str) -> ToolResult:
        if not old_text:
            raise ValueError("old_text cannot be empty")
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"Path escapes workspace: {path}")
        with target.open("r", encoding="utf-8", newline="") as handle:
            original = handle.read()
        matches = original.count(old_text)
        if matches == 0:
            raise ValueError("old_text was not found; file was not changed")
        if matches != 1:
            raise ValueError(
                f"old_text matched {matches} occurrences; file was not changed"
            )
        updated = original.replace(old_text, new_text, 1)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.chmod(temporary_path, target.stat().st_mode)
            os.replace(temporary_path, target)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return ToolResult(
            content=f"Replaced one exact occurrence in {path}",
            metadata={"path": path, "characters": len(updated)},
        )

    return Tool(
        name="edit_file",
        description=(
            "Replace one exact old_text occurrence in a UTF-8 workspace file. "
            "Fails without writing when the match count is not exactly one."
        ),
        parameters=object_schema(
            {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text that must occur once",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            required=["path", "old_text", "new_text"],
        ),
        handler=edit_file,
        metadata={"effects": ["write"]},
    )


__all__ = ["create_edit_file_tool"]
