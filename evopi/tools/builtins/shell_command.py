"""Workspace-scoped shell command tool (not a security sandbox)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

from evopi.core.tool import Tool, ToolResult
from evopi.tools.schema import object_schema
from evopi.tools.shell_environment import (
    ShellEnvironment,
    resolve_shell_environment,
)


def create_shell_command_tool(
    workspace: str | Path,
    *,
    timeout: float = 60.0,
    abort_grace_period: float = 1.0,
    shell_environment: ShellEnvironment | None = None,
) -> Tool:
    root = Path(workspace).resolve()
    environment = shell_environment or resolve_shell_environment()
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
        process_environment: dict[str, str] | None = None
        process_argv = environment.argv(command)
        if environment.kind == "cmd":
            # CPython quotes exec argv with C-runtime rules on Windows, but
            # cmd.exe does not decode embedded \" sequences. Transport the
            # reviewed command through this child process's private environment.
            process_environment = os.environ.copy()
            process_environment["EVOPI_SHELL_COMMAND"] = command
            process_argv = (
                environment.executable,
                "/d",
                "/s",
                "/c",
                "%EVOPI_SHELL_COMMAND%",
            )
            process_options["env"] = process_environment
        process = await asyncio.create_subprocess_exec(
            *process_argv,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_options,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            await _terminate_process_tree(process, grace_period=abort_grace_period)
            raise

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
            f"Run a command through {environment.display_name} "
            f"({environment.executable}) with the workspace as its current directory. "
            "This tool is governed by shell safety policies but is not a sandbox."
        ),
        parameters=object_schema(
            {"command": {"type": "string", "description": "Command to execute"}},
            required=["command"],
        ),
        handler=shell_command,
        timeout=timeout,
        timeout_grace_period=abort_grace_period,
        metadata={
            "effects": ["execute"],
            "shell_mode": environment.requested_mode,
            "shell_kind": environment.kind,
            "shell_executable": environment.executable,
            "prompt_guidelines": [environment.syntax_guideline],
        },
    )


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float,
) -> None:
    if process.returncode is not None:
        return

    if sys.platform == "win32":
        with suppress(ProcessLookupError, OSError, ValueError):
            process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        with suppress(ProcessLookupError, PermissionError):
            getattr(os, "killpg")(process.pid, signal.SIGTERM)

    try:
        await asyncio.wait_for(process.wait(), timeout=grace_period)
    except TimeoutError:
        if sys.platform == "win32":
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
