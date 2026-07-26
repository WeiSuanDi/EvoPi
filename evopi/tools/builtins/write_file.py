"""UTF-8 text file writing tool."""

from __future__ import annotations

from pathlib import Path

from evopi.core.tool import Tool, ToolResult
from evopi.tools.schema import object_schema


def create_write_file_tool(workspace: str | Path) -> Tool:
    root = Path(workspace).resolve()

    def write_file(path: str, content: str) -> ToolResult:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"Path escapes workspace: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            content=f"Wrote {len(content)} characters to {path}",
            metadata={"path": path, "characters": len(content)},
        )

    return Tool(
        name="write_file",
        description="Create or replace a UTF-8 text file inside the workspace.",
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Workspace-relative file path"},
                "content": {"type": "string", "description": "Complete new file content"},
            },
            required=["path", "content"],
        ),
        handler=write_file,
        metadata={"effects": ["write"]},
    )


__all__ = ["create_write_file_tool"]
