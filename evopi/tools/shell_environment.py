"""Resolved, host-visible shell execution environment."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

ShellMode: TypeAlias = Literal["auto", "cmd", "powershell"]
ShellKind: TypeAlias = Literal["cmd", "powershell", "posix-sh"]


@dataclass(slots=True, frozen=True, kw_only=True)
class ShellEnvironment:
    """One resolved shell whose syntax and executable are explicit."""

    requested_mode: ShellMode
    kind: ShellKind
    executable: str
    platform: str

    def argv(self, command: str) -> tuple[str, ...]:
        if self.kind == "cmd":
            return (self.executable, "/d", "/s", "/c", command)
        if self.kind == "powershell":
            return (
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            )
        return (self.executable, "-c", command)

    @property
    def display_name(self) -> str:
        return {
            "cmd": "Windows cmd.exe",
            "powershell": "PowerShell",
            "posix-sh": "POSIX /bin/sh",
        }[self.kind]

    @property
    def syntax_guideline(self) -> str:
        return {
            "cmd": (
                "Use Windows cmd.exe syntax. Do not use PowerShell cmdlets or "
                "POSIX shell syntax."
            ),
            "powershell": (
                "Use PowerShell syntax and cmdlets. Do not use cmd.exe or "
                "POSIX shell syntax."
            ),
            "posix-sh": (
                "Use POSIX /bin/sh syntax. Do not use cmd.exe or "
                "PowerShell-specific syntax."
            ),
        }[self.kind]


def resolve_shell_environment(
    mode: str = "auto",
    *,
    platform: str | None = None,
) -> ShellEnvironment:
    """Resolve one supported shell before a Session or model call starts."""

    if mode not in {"auto", "cmd", "powershell"}:
        raise ValueError(f"Unsupported shell mode: {mode}")
    requested_mode = cast(ShellMode, mode)
    resolved_platform = platform or sys.platform
    is_windows = resolved_platform.startswith("win")
    if mode == "auto":
        if not is_windows:
            return ShellEnvironment(
                requested_mode=requested_mode,
                kind="posix-sh",
                executable="/bin/sh",
                platform=resolved_platform,
            )
        return _resolve_cmd(requested_mode, resolved_platform)
    if mode == "cmd":
        return _resolve_cmd(requested_mode, resolved_platform)

    executable = shutil.which("pwsh")
    if executable is None and is_windows:
        executable = shutil.which("powershell.exe")
    if executable is None:
        raise ValueError(
            "PowerShell mode requires 'pwsh' or Windows 'powershell.exe'"
        )
    return ShellEnvironment(
        requested_mode=requested_mode,
        kind="powershell",
        executable=executable,
        platform=resolved_platform,
    )


def _resolve_cmd(mode: ShellMode, platform: str) -> ShellEnvironment:
    executable = shutil.which("cmd.exe")
    if executable is None:
        raise ValueError("cmd mode requires 'cmd.exe'")
    return ShellEnvironment(
        requested_mode=mode,
        kind="cmd",
        executable=executable,
        platform=platform,
    )


__all__ = [
    "ShellEnvironment",
    "ShellKind",
    "ShellMode",
    "resolve_shell_environment",
]
