"""Workspace-scoped shell command tool (not a security sandbox)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from evopi.core.tool import Tool, ToolResult
from evopi.tools.schema import object_schema


def create_shell_command_tool(workspace: str | Path, *, timeout: float = 60.0) -> Tool:
    root = Path(workspace).resolve()

    async def shell_command(command: str) -> ToolResult:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult(
                content=f"Command timed out after {timeout:g} seconds",
                is_error=True,
                metadata={"timeout": timeout},
            )

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")
        content = stdout_text
        if stderr_text:
            content += ("\n" if content else "") + stderr_text
        return ToolResult(
            content=content.rstrip(),
            is_error=process.returncode != 0,
            metadata={"exit_code": process.returncode},
        )

    return Tool(
        name="shell_command",
        description=(
            "Run a shell command with the workspace as its current directory. "
            "This tool is governed by shell safety policies but is not a sandbox."
        ),
        parameters=object_schema(
            {"command": {"type": "string", "description": "Command to execute"}},
            required=["command"],
        ),
        handler=shell_command,
    )


__all__ = ["create_shell_command_tool"]
