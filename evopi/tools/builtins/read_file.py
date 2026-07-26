"""UTF-8 text file reading tool."""

from __future__ import annotations

from pathlib import Path

from evopi.core.tool import Tool
from evopi.tools.schema import object_schema


def create_read_file_tool(workspace: str | Path) -> Tool:
    root = Path(workspace).resolve()

    def read_file(path: str) -> str:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"Path escapes workspace: {path}")
        return target.read_text(encoding="utf-8")

    return Tool(
        name="read_file",
        description="Read a UTF-8 text file inside the workspace.",
        parameters=object_schema(
            {"path": {"type": "string", "description": "Workspace-relative file path"}},
            required=["path"],
        ),
        handler=read_file,
        metadata={"effects": ["read"]},
    )


__all__ = ["create_read_file_tool"]
