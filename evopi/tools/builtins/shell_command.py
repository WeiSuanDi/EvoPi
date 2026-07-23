"""Workspace-scoped shell command tool (not a security sandbox)."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

from evopi.core.tool import Tool, ToolResult
from evopi.tools.schema import object_schema


def create_shell_command_tool(
    workspace: str | Path,
    *,
    timeout: float = 60.0,
    abort_grace_period: float = 1.0,
) -> Tool:
    root = Path(workspace).resolve()
    if abort_grace_period < 0:
        raise ValueError("abort_grace_period cannot be negative")

    async def shell_command(command: str) -> ToolResult:
        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        else:
            process_options["start_new_session"] = True
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_options,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            await _terminate_process_tree(process, grace_period=abort_grace_period)
            raise
        except TimeoutError:
            await _terminate_process_tree(process, grace_period=abort_grace_period)
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


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float,
) -> None:
    if process.returncode is not None:
        return

    if os.name == "nt":
        with suppress(ProcessLookupError, OSError, ValueError):
            process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        with suppress(ProcessLookupError, PermissionError):
            getattr(os, "killpg")(process.pid, signal.SIGTERM)

    try:
        await asyncio.wait_for(process.wait(), timeout=grace_period)
    except TimeoutError:
        if os.name == "nt":
            await _force_kill_windows_tree(process.pid)
        else:
            with suppress(ProcessLookupError, PermissionError):
                getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()

    with suppress(Exception):
        await process.communicate()


async def _force_kill_windows_tree(pid: int) -> None:
    with suppress(OSError):
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()


__all__ = ["create_shell_command_tool"]
