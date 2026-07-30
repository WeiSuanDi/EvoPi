from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evopi.tools import ShellEnvironment, resolve_shell_environment
from evopi.tools.builtins import create_shell_command_tool


def test_auto_resolves_cmd_on_windows_and_posix_shell_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "evopi.tools.shell_environment.shutil.which",
        lambda name: "C:\\Windows\\System32\\cmd.exe" if name == "cmd.exe" else None,
    )

    windows = resolve_shell_environment("auto", platform="win32")
    posix = resolve_shell_environment("auto", platform="linux")

    assert windows == ShellEnvironment(
        requested_mode="auto",
        kind="cmd",
        executable="C:\\Windows\\System32\\cmd.exe",
        platform="win32",
    )
    assert posix == ShellEnvironment(
        requested_mode="auto",
        kind="posix-sh",
        executable="/bin/sh",
        platform="linux",
    )


def test_powershell_prefers_pwsh_then_windows_powershell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found: dict[str, str | None] = {
        "pwsh": "C:\\Program Files\\PowerShell\\7\\pwsh.exe",
        "powershell.exe": (
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
        ),
    }
    monkeypatch.setattr(
        "evopi.tools.shell_environment.shutil.which",
        lambda name: found.get(name),
    )

    preferred = resolve_shell_environment("powershell", platform="win32")
    assert preferred.executable.endswith("pwsh.exe")

    found["pwsh"] = None
    fallback = resolve_shell_environment("powershell", platform="win32")
    assert fallback.executable.endswith("powershell.exe")

    found["powershell.exe"] = None
    with pytest.raises(ValueError, match="PowerShell"):
        resolve_shell_environment("powershell", platform="win32")


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (
            ShellEnvironment(
                requested_mode="cmd",
                kind="cmd",
                executable="cmd.exe",
                platform="win32",
            ),
            (
                "cmd.exe",
                "/d",
                "/s",
                "/c",
                "%EVOPI_SHELL_COMMAND%",
            ),
        ),
        (
            ShellEnvironment(
                requested_mode="powershell",
                kind="powershell",
                executable="pwsh",
                platform="win32",
            ),
            (
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "echo ok",
            ),
        ),
        (
            ShellEnvironment(
                requested_mode="auto",
                kind="posix-sh",
                executable="/bin/sh",
                platform="linux",
            ),
            ("/bin/sh", "-c", "echo ok"),
        ),
    ],
)
def test_shell_tool_uses_explicit_argv_and_publishes_actual_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: ShellEnvironment,
    expected: tuple[str, ...],
) -> None:
    observed: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"stdout", b"stderr"

    async def create(*args: str, **kwargs: object) -> Process:
        observed.append((args, kwargs))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    tool = create_shell_command_tool(tmp_path, shell_environment=environment)

    result = asyncio.run(tool.execute({"command": "echo ok"}))

    assert observed[0][0] == expected
    assert observed[0][1]["cwd"] == tmp_path.resolve()
    if environment.kind == "cmd":
        process_environment = observed[0][1]["env"]
        assert isinstance(process_environment, dict)
        assert process_environment["EVOPI_SHELL_COMMAND"] == "echo ok"
    else:
        assert "env" not in observed[0][1]
    assert result.content == "stdout\nstderr"
    assert result.metadata["exit_code"] == 0
    assert tool.metadata["shell_kind"] == environment.kind
    assert tool.metadata["shell_executable"] == environment.executable
    assert environment.executable in tool.description
    assert environment.syntax_guideline in tool.metadata["prompt_guidelines"]


def test_shell_environment_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="Unsupported shell mode"):
        resolve_shell_environment("fish")  # type: ignore[arg-type]
