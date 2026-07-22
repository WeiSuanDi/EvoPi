from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

from evopi.coding.harness import CodingHarness
from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import ToolCall
from evopi.core.tool import Tool
from evopi.harness.confirmation import ConfirmationRequest, ConfirmationResponse
from evopi.trace.reader import read_trace


class ScriptedModel:
    name = "coding-script"

    def __init__(self, messages: list[AssistantMessage]) -> None:
        self._messages = iter(messages)

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        yield ModelComplete(message=next(self._messages))


def approve(request: ConfirmationRequest) -> ConfirmationResponse:
    return ConfirmationResponse(request_id=request.id, decision="approve")


def test_coding_harness_writes_runs_and_traces_demo(tmp_path) -> None:
    command = f'"{sys.executable}" hello.py'
    model = ScriptedModel(
        [
            AssistantMessage(
                content="I will create the file.",
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={"path": "hello.py", "content": "print('hello EvoPi')\n"},
                    )
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(
                content="I will run it.",
                tool_calls=[
                    ToolCall(
                        id="shell-1",
                        name="shell_command",
                        arguments={"command": command},
                    )
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="Created and ran hello.py successfully.", stop_reason="stop"),
        ]
    )
    trace_path = tmp_path / "trace.jsonl"
    harness = CodingHarness(
        model=model,
        workspace=tmp_path,
        trace_path=trace_path,
        confirmation_handler=approve,
    )

    answer = asyncio.run(harness.prompt("Create hello.py and run it"))

    assert answer.content == "Created and ran hello.py successfully."
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello EvoPi')\n"
    results = [message for message in harness.messages if isinstance(message, ToolResultMessage)]
    assert results[0].tool_name == "write_file"
    assert results[1].content == "hello EvoPi"
    records = list(read_trace(trace_path))
    assert sum(record["type"] == "tool_call" for record in records) == 2
    assert sum(record["type"] == "confirmation_request" for record in records) == 1
    assert records[-2]["type"] == "agent_end" or records[-1]["type"] == "agent_end"


def test_coding_policies_block_escape_and_truncate_before_model_feedback(tmp_path) -> None:
    write_executed = False

    def unsafe_write(path: str, content: str) -> str:
        nonlocal write_executed
        write_executed = True
        return path

    model = ScriptedModel(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="write-escape",
                        name="write_file",
                        arguments={"path": "../escape.txt", "content": "unsafe"},
                    )
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="shell-long",
                        name="shell_command",
                        arguments={"command": "safe-command"},
                    )
                ],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="handled", stop_reason="stop"),
        ]
    )
    harness = CodingHarness(
        model=model,
        workspace=tmp_path,
        max_output_chars=5,
        confirmation_handler=approve,
    )
    harness.register_tool(
        Tool(
            name="write_file",
            description="Test write",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            handler=unsafe_write,
        ),
        replace=True,
    )
    harness.register_tool(
        Tool(
            name="shell_command",
            description="Test shell",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            handler=lambda command: "0123456789",
        ),
        replace=True,
    )

    asyncio.run(harness.prompt("exercise coding policies"))

    results = [message for message in harness.messages if isinstance(message, ToolResultMessage)]
    assert write_executed is False
    assert results[0].is_error is True
    assert results[0].metadata["blocked"] is True
    assert results[1].content.startswith("01234")
    assert results[1].metadata["truncated"] is True
