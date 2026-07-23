from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from importlib import import_module

import pytest

from evopi.ai.api.base import ModelRequestError
from evopi.cli.confirmation import (
    async_terminal_confirmation_handler,
    terminal_confirmation_handler,
)
from evopi.core.agent_loop import AgentLoop
from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import ToolCall
from evopi.harness.confirmation import ConfirmationRequest
from evopi.trace.reader import read_trace

cli_main = import_module("evopi.cli.main")


class ShellModel:
    name = "shell-model"

    def __init__(self) -> None:
        self._messages = iter(
            [
                AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="shell-1",
                            name="shell_command",
                            arguments={"command": "echo cli-confirmed"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(content="done", stop_reason="stop"),
            ]
        )

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        yield ModelComplete(message=next(self._messages))


class RetryModel:
    name = "retry-model"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.calls += 1
        if self.calls == 1:
            raise ModelRequestError(
                "temporary outage",
                kind="server",
                provider="test",
            )
        yield ModelComplete(message=AssistantMessage(content="done", stop_reason="stop"))


def test_terminal_handler_renders_request_and_approves_yes() -> None:
    output: list[str] = []
    request = ConfirmationRequest(
        hook="before_tool_call",
        reason="Tool requires confirmation",
        risk_level="medium",
        tool_call=ToolCall(
            id="shell-1",
            name="shell_command",
            arguments={"command": "python -m pytest"},
        ),
        arguments={"command": "python -m pytest -q"},
    )

    response = terminal_confirmation_handler(
        request,
        input_fn=lambda prompt: "yes",
        output_fn=output.append,
    )

    assert response.approved is True
    rendered = "\n".join(output)
    assert "Tool: shell_command" in rendered
    assert "Risk: medium" in rendered
    assert "python -m pytest -q" in rendered


def test_terminal_handler_defaults_to_deny() -> None:
    request = ConfirmationRequest(hook="before_tool_call", reason="confirm")

    response = terminal_confirmation_handler(
        request,
        input_fn=lambda prompt: "",
        output_fn=lambda line: None,
    )

    assert response.approved is False
    assert response.reason == "Denied by user"


def test_async_terminal_handler_uses_prompt_session() -> None:
    class FakeSession:
        async def prompt_async(self, prompt: str) -> str:
            assert prompt == "Approve? [y/N]: "
            return "yes"

    request = ConfirmationRequest(hook="before_tool_call", reason="confirm")

    response = asyncio.run(
        async_terminal_confirmation_handler(
            request,
            session=FakeSession(),  # type: ignore[arg-type]
            output_fn=lambda line: None,
        )
    )

    assert response.decision == "approve"


def test_async_terminal_handler_maps_keyboard_interrupt_to_cancelled() -> None:
    class InterruptingSession:
        async def prompt_async(self, prompt: str) -> str:
            raise KeyboardInterrupt

    request = ConfirmationRequest(hook="before_tool_call", reason="confirm")

    response = asyncio.run(
        async_terminal_confirmation_handler(
            request,
            session=InterruptingSession(),  # type: ignore[arg-type]
            output_fn=lambda line: None,
        )
    )

    assert response.decision == "cancelled"
    assert response.reason == "Cancelled by user"


def test_cli_injects_confirmation_handler_and_runs_approved_shell(
    tmp_path, monkeypatch
) -> None:
    trace_path = tmp_path / "cli-trace.jsonl"
    args = argparse.Namespace(
        prompt="run a command",
        provider=None,
        workspace=tmp_path,
        trace=trace_path,
    )
    monkeypatch.setattr(cli_main, "model_from_environment", lambda provider: ShellModel())

    async def approve(request: ConfirmationRequest, *, signal=None):
        return terminal_confirmation_handler(
            request,
            input_fn=lambda prompt: "y",
            output_fn=lambda line: None,
        )

    monkeypatch.setattr(cli_main, "async_terminal_confirmation_handler", approve)

    exit_code = asyncio.run(cli_main._run(args))

    assert exit_code == 0
    records = list(read_trace(trace_path))
    response = next(item for item in records if item["type"] == "confirmation_response")
    assert response["data"]["response"]["decision"] == "approve"
    tool_result = next(
        item for item in records if item["type"] == "tool_execution_end"
    )
    assert "cli-confirmed" in tool_result["data"]["result"]["content"]


def test_main_returns_130_for_keyboard_interrupt(monkeypatch, capsys) -> None:
    class Parser:
        def parse_args(self):
            return argparse.Namespace()

    async def interrupt(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "build_parser", lambda: Parser())
    monkeypatch.setattr(cli_main, "_run", interrupt)

    assert cli_main.main() == 130
    assert "EvoPi aborted." in capsys.readouterr().out


def test_cli_retry_flags_validate_values() -> None:
    parser = cli_main.build_parser()
    args = parser.parse_args(
        ["task", "--no-retry", "--max-retries", "5", "--model-timeout", "30"]
    )
    assert args.no_retry is True
    assert args.max_retries == 5
    assert args.model_timeout == 30

    with pytest.raises(SystemExit):
        parser.parse_args(["task", "--max-retries", "-1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["task", "--model-timeout", "0"])


def test_cli_retries_by_default_and_reports_to_stderr(tmp_path, monkeypatch, capsys) -> None:
    model = RetryModel()
    captured_timeout: list[float] = []

    def factory(provider, *, timeout):
        captured_timeout.append(timeout)
        return model

    async def no_wait(delay, signal):
        return True

    monkeypatch.setattr(cli_main, "model_from_environment", factory)
    monkeypatch.setattr(AgentLoop, "_wait_for_retry", staticmethod(no_wait))
    args = argparse.Namespace(
        prompt="retry",
        provider=None,
        workspace=tmp_path,
        trace=tmp_path / "retry.jsonl",
        no_retry=False,
        max_retries=3,
        model_timeout=9.0,
    )

    assert asyncio.run(cli_main._run(args)) == 0

    output = capsys.readouterr()
    assert model.calls == 2
    assert captured_timeout == [9.0]
    assert "retrying model" in output.err
    assert "server" in output.err
