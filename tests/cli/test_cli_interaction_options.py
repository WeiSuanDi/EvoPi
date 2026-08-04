"""CLI/environment resolution tests for interaction queue modes (SFU-2 Task 3)."""

from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from evopi.cli.product import resolve_interaction_modes
from evopi.cli.repl import (
    ReplCommandContext,
    ReplCommandRegistry,
    ReplStartupConfig,
    build_repl_startup_config,
    startup_panel,
)
from evopi.coding import CodingHarness
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete


class _Model:
    name = "test-model"
    context_window = 0

    async def stream(self, context):
        yield ModelComplete(
            message=AssistantMessage(content="done", stop_reason="stop")
        )


def test_default_modes_are_one_at_a_time(monkeypatch) -> None:
    monkeypatch.delenv("EVOPI_STEERING_MODE", raising=False)
    monkeypatch.delenv("EVOPI_FOLLOW_UP_MODE", raising=False)

    assert resolve_interaction_modes(SimpleNamespace()) == (
        "one-at-a-time",
        "one-at-a-time",
    )


def test_environment_modes_apply_when_no_cli_flag(monkeypatch) -> None:
    monkeypatch.setenv("EVOPI_STEERING_MODE", "all")
    monkeypatch.setenv("EVOPI_FOLLOW_UP_MODE", "one-at-a-time")

    assert resolve_interaction_modes(SimpleNamespace()) == (
        "all",
        "one-at-a-time",
    )


def test_cli_overrides_environment(monkeypatch) -> None:
    monkeypatch.setenv("EVOPI_STEERING_MODE", "all")
    monkeypatch.setenv("EVOPI_FOLLOW_UP_MODE", "all")
    args = SimpleNamespace(steering_mode="one-at-a-time", follow_up_mode="all")

    assert resolve_interaction_modes(args) == ("one-at-a-time", "all")


def test_invalid_environment_modes_raise_strictly(monkeypatch) -> None:
    monkeypatch.setenv("EVOPI_STEERING_MODE", "sometimes")
    with pytest.raises(ValueError, match="EVOPI_STEERING_MODE"):
        resolve_interaction_modes(SimpleNamespace())

    monkeypatch.setenv("EVOPI_STEERING_MODE", "all")
    monkeypatch.setenv("EVOPI_FOLLOW_UP_MODE", "batch")
    with pytest.raises(ValueError, match="EVOPI_FOLLOW_UP_MODE"):
        resolve_interaction_modes(SimpleNamespace())


def test_parser_exposes_mode_flags() -> None:
    from evopi.cli.main import build_parser

    args = build_parser().parse_args(
        ["--steering-mode", "all", "--follow-up-mode", "one-at-a-time"]
    )
    assert args.steering_mode == "all"
    assert args.follow_up_mode == "one-at-a-time"


def test_parser_rejects_unknown_modes() -> None:
    from evopi.cli.main import build_parser

    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["--steering-mode", "sometimes"])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["--follow-up-mode", "batch"])
    assert error.value.code == 2


def test_repl_startup_config_carries_resolved_modes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EVOPI_STEERING_MODE", "all")
    monkeypatch.setenv("EVOPI_FOLLOW_UP_MODE", "one-at-a-time")
    harness = CodingHarness(model=_Model(), workspace=tmp_path, memory_path=None)

    config = build_repl_startup_config(SimpleNamespace(), harness)

    assert config.steering_mode == "all"
    assert config.follow_up_mode == "one-at-a-time"


def test_startup_panel_shows_modes_without_content(tmp_path) -> None:
    console = Console(file=StringIO(), force_terminal=False, width=120)
    harness = CodingHarness(model=_Model(), workspace=tmp_path, memory_path=None)
    config = ReplStartupConfig(
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
        shell_mode="auto",
        shell_kind="cmd",
        shell_executable="cmd.exe",
        steering_mode="all",
        follow_up_mode="one-at-a-time",
    )
    context = ReplCommandContext(
        harness=harness,
        startup=config,
        display=None,
        console=console,
    )

    panel = startup_panel(context)

    rendered = panel.renderable.__str__()
    assert "steer all" in rendered
    assert "follow-up one-at-a-time" in rendered


def test_status_and_settings_show_modes(tmp_path) -> None:
    registry = ReplCommandRegistry()
    output = StringIO()
    harness = CodingHarness(model=_Model(), workspace=tmp_path, memory_path=None)
    config = ReplStartupConfig(
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
        shell_mode="auto",
        shell_kind="cmd",
        shell_executable="cmd.exe",
        steering_mode="all",
        follow_up_mode="one-at-a-time",
    )
    context = ReplCommandContext(
        harness=harness,
        startup=config,
        display=None,
        console=Console(file=output, force_terminal=False, width=120),
    )

    asyncio.run(registry.dispatch(context, "/status"))
    asyncio.run(registry.dispatch(context, "/settings"))

    rendered = output.getvalue()
    assert "Steering mode" in rendered
    assert "Follow-up mode" in rendered
    assert "all" in rendered
    assert "one-at-a-time" in rendered


def test_status_shows_interaction_snapshot_counts(tmp_path) -> None:
    registry = ReplCommandRegistry()
    output = StringIO()
    harness = CodingHarness(model=_Model(), workspace=tmp_path, memory_path=None)
    context = ReplCommandContext(
        harness=harness,
        startup=_startup_config(tmp_path),
        display=None,
        console=Console(file=output, force_terminal=False, width=120),
    )

    asyncio.run(registry.dispatch(context, "/status"))
    rendered = output.getvalue()
    assert "Pending steering" in rendered
    assert "Pending follow-up" in rendered
    assert "0" in rendered


def test_mode_flags_reach_every_product_route(monkeypatch) -> None:
    """The flags parse and reach the Harness-building step on every route."""
    import importlib

    cli_main = importlib.import_module("evopi.cli.main")
    seen: dict[str, object] = {}

    def fake_build(args, **kwargs):
        seen["args"] = args
        return SimpleNamespace()

    async def fake_run(harness, broker):
        return 0

    async def fake_one_shot(args, *, json_output=False):
        seen["args"] = args
        return 0

    async def fake_repl(args, *, initial_prompt=None):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli_main, "_build_harness", fake_build)
    monkeypatch.setattr(cli_main, "run_stdio_rpc", fake_run)
    monkeypatch.setattr(cli_main, "_run_one_shot", fake_one_shot)
    monkeypatch.setattr(cli_main, "_run_repl", fake_repl)
    monkeypatch.setattr(cli_main.sys, "stdin", StringIO(""))

    assert cli_main.main(["rpc", "--steering-mode", "all", "--no-session"]) == 0
    assert seen["args"].steering_mode == "all"  # type: ignore[union-attr]
    assert (
        cli_main.main(
            ["run", "--follow-up-mode", "all", "--no-session", "prompt"]
        )
        == 0
    )
    assert seen["args"].follow_up_mode == "all"  # type: ignore[union-attr]
    assert cli_main.main(["chat", "--steering-mode", "all", "--no-session"]) == 0
    assert seen["args"].steering_mode == "all"  # type: ignore[union-attr]


def _startup_config(tmp_path) -> ReplStartupConfig:
    return ReplStartupConfig(
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
        shell_mode="auto",
        shell_kind="cmd",
        shell_executable="cmd.exe",
    )
