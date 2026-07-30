from __future__ import annotations

import asyncio
from io import StringIO
from uuid import uuid4

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import Console

from evopi.cli.repl import (
    ReplCommandContext,
    ReplCommandRegistry,
    ReplCommandResult,
    ReplCompleter,
    ReplStartupConfig,
)
from evopi.coding import CodingHarness
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete
from evopi.session import build_runtime_fingerprint


class _Model:
    name = "test-model"
    context_window = 0

    async def stream(self, context):
        yield ModelComplete(
            message=AssistantMessage(content="done", stop_reason="stop")
        )


def _context(tmp_path, *, recent_prompt: str | None = None):
    harness = CodingHarness(
        model=_Model(),
        workspace=tmp_path,
        memory_path=None,
    )
    output = StringIO()
    return ReplCommandContext(
        harness=harness,
        startup=ReplStartupConfig(
            provider="test",
            model="test-model",
            base_url="https://example.test",
            workspace=str(tmp_path),
            session_mode="memory",
            retry_enabled=True,
            max_retries=3,
            deadline=None,
            tool_timeout=None,
            fallbacks=(),
            included_tools=None,
            excluded_tools=None,
        ),
        display=None,
        console=Console(file=output, force_terminal=False, width=120),
        recent_prompt=recent_prompt,
    ), output


def test_builtin_registry_is_the_single_reserved_name_source() -> None:
    registry = ReplCommandRegistry()

    assert registry.command_names == (
        "/agents",
        "/branch",
        "/clear",
        "/compact",
        "/exit",
        "/fork",
        "/help",
        "/leaves",
        "/memory",
        "/merge",
        "/new",
        "/plugins",
        "/policies",
        "/quit",
        "/reload",
        "/retry",
        "/session",
        "/settings",
        "/skills",
        "/status",
        "/switch",
        "/tools",
        "/trace",
        "/tree",
    )
    assert registry.reserved_plugin_commands == frozenset(registry.command_names)


def test_retry_state_is_context_owned_and_returns_an_explicit_outcome(tmp_path) -> None:
    registry = ReplCommandRegistry()
    first, _ = _context(tmp_path / "first", recent_prompt="repeat me")
    second, output = _context(tmp_path / "second")

    retry = asyncio.run(registry.dispatch(first, "/retry"))
    missing = asyncio.run(registry.dispatch(second, "/retry"))

    assert retry == ReplCommandResult(action="retry", prompt="repeat me")
    assert missing == ReplCommandResult()
    assert "No previous prompt" in output.getvalue()


def test_quit_and_exit_are_registry_outcomes(tmp_path) -> None:
    registry = ReplCommandRegistry()
    context, _ = _context(tmp_path)

    assert asyncio.run(registry.dispatch(context, "/quit")).action == "quit"
    assert asyncio.run(registry.dispatch(context, "/exit")).action == "quit"


def test_new_uses_harness_reset_and_preserves_old_session(tmp_path) -> None:
    registry = ReplCommandRegistry()
    context, output = _context(tmp_path)
    old_id = context.harness.session.session_id

    result = asyncio.run(registry.dispatch(context, "/new"))

    assert result.action == "continue"
    assert context.harness.session.session_id != old_id
    assert "New session" in output.getvalue()


def test_help_is_grouped_and_can_describe_one_command(tmp_path) -> None:
    registry = ReplCommandRegistry()
    context, output = _context(tmp_path)

    asyncio.run(registry.dispatch(context, "/help"))
    all_help = output.getvalue()
    assert "Runtime" in all_help
    assert "Session" in all_help
    assert "Governance" in all_help
    assert "Resources" in all_help
    assert "Plugin" in all_help

    output.seek(0)
    output.truncate(0)
    asyncio.run(registry.dispatch(context, "/help tools"))
    assert "/tools [active|all]" in output.getvalue()


def test_status_and_resource_commands_use_public_snapshots(tmp_path) -> None:
    registry = ReplCommandRegistry()
    context, output = _context(tmp_path)

    for command in (
        "/status",
        "/settings",
        "/tools active",
        "/policies",
        "/plugins",
        "/skills",
        "/memory",
        "/agents",
        "/trace",
        "/session",
    ):
        asyncio.run(registry.dispatch(context, command))

    rendered = output.getvalue()
    assert "test-model" in rendered
    assert "Active tools" in rendered
    assert "Turn budget" in rendered
    assert "Max turns" in rendered
    assert "Memory" in rendered
    assert "private" not in rendered


def test_completer_uses_registry_plugin_commands_and_leaf_prefixes(tmp_path) -> None:
    registry = ReplCommandRegistry()
    context, _ = _context(tmp_path)
    run_id = uuid4().hex
    context.harness.session.append_run_start(
        run_id=run_id,
        runtime_fingerprint=build_runtime_fingerprint(
            harness="test",
            model="test",
            system_prompt="",
            tools=[],
            policies=[],
        ),
    )
    context.harness.session.append_run_end(run_id=run_id, reason="completed")
    leaf = context.harness.session.leaf_id
    assert leaf is not None
    completer = ReplCompleter(registry=registry, context=context)

    command_items = list(
        completer.get_completions(
            Document("/to"),
            CompleteEvent(completion_requested=True),
        )
    )
    leaf_items = list(
        completer.get_completions(
            Document("/switch "),
            CompleteEvent(completion_requested=True),
        )
    )

    assert any(item.text == "/tools" for item in command_items)
    assert any(item.text == leaf[:16] for item in leaf_items)
