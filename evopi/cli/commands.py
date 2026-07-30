"""Backward-compatible REPL command dispatch facade."""

from __future__ import annotations

import sys
from typing import cast

from rich.console import Console

from evopi.cli.repl import (
    ReplCommandContext,
    ReplCommandRegistry,
    ReplCommandResult,
    ReplStartupConfig,
)
from evopi.coding import CodingHarness
from evopi.harness import BaseHarness


async def handle_slash_command(
    harness: BaseHarness,
    text: str,
) -> ReplCommandResult:
    """Dispatch through the registry for callers using the legacy helper."""

    coding_harness = cast(CodingHarness, harness)
    shell_environment = getattr(coding_harness, "shell_environment", None)
    context = ReplCommandContext(
        harness=coding_harness,
        startup=ReplStartupConfig(
            provider="unknown",
            model=harness.model.name,
            base_url=str(getattr(harness.model, "base_url", "-")),
            workspace=str(harness.session.attached_workspace),
            session_mode=(
                "persistent" if harness.session.is_persistent else "memory"
            ),
            retry_enabled=harness.agent.retry_config.enabled,
            max_retries=harness.agent.retry_config.max_retries,
            deadline=harness.deadline,
            tool_timeout=harness.tool_timeout,
            fallbacks=(),
            included_tools=None,
            excluded_tools=None,
            max_turns=harness.agent.max_turns,
            shell_mode=(
                shell_environment.requested_mode
                if shell_environment is not None
                else "-"
            ),
            shell_kind=(
                shell_environment.kind if shell_environment is not None else "-"
            ),
            shell_executable=(
                shell_environment.executable
                if shell_environment is not None
                else "-"
            ),
        ),
        display=None,
        console=Console(file=sys.stderr),
    )
    return await ReplCommandRegistry().dispatch(context, text)


__all__ = ["handle_slash_command"]
