"""Directory listing tool."""

from __future__ import annotations

from pathlib import Path

from evopi.core.tool import Tool
from evopi.tools.schema import object_schema


def _resolve_inside(root: Path, value: str) -> Path:
    root = root.resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path escapes workspace: {value}")
    return path


def create_list_dir_tool(workspace: str | Path) -> Tool:
    root = Path(workspace).resolve()

    def list_dir(path: str = ".") -> str:
        target = _resolve_inside(root, path)
        if not target.is_dir():
            raise NotADirectoryError(path)
        entries = [item.name + ("/" if item.is_dir() else "") for item in target.iterdir()]
        return "\n".join(sorted(entries, key=str.casefold))

    return Tool(
        name="list_dir",
        description="List files and directories inside the workspace.",
        parameters=object_schema(
            {"path": {"type": "string", "description": "Workspace-relative directory"}}
        ),
        handler=list_dir,
        metadata={"effects": ["read"]},
    )


__all__ = ["create_list_dir_tool"]
