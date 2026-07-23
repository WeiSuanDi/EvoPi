from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from importlib import import_module

from evopi.cli.confirmation import (
    async_terminal_confirmation_handler,
    terminal_confirmation_handler,
)
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
