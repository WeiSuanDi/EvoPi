from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from threading import Event, Thread

import pytest

from evopi.core.agent import Agent
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent, TextDelta, ToolCallDelta
from evopi.core.tool import Tool, ToolCall, ToolResult
from evopi.tools.builtins.shell_command import create_shell_command_tool


class BlockingTextModel:
    name = "blocking-text"

    def __init__(self) -> None:
        self.blocking = asyncio.Event()

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        yield TextDelta(delta="partial")
        self.blocking.set()
        await asyncio.Event().wait()


class ToolBatchModel:
    name = "tool-batch"

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        yield ModelComplete(
            message=AssistantMessage(
                content="working",
                tool_calls=[
                    ToolCall(id="call-1", name="work", arguments={}),
                    ToolCall(id="call-2", name="work", arguments={}),
                ],
                stop_reason="tool_use",
            )
        )


def test_abort_during_model_stream_keeps_partial_text_and_event_order() -> None:
    async def scenario() -> None:
        model = BlockingTextModel()
        events: list[CoreEvent] = []
        observed_signal: list[bool] = []
        agent = Agent(model=model)

        def listener(event: CoreEvent, *, signal=None) -> None:
            events.append(event)
            if event.type == "abort_requested":
                observed_signal.append(bool(signal and signal.aborted))

        agent.subscribe(listener)
        prompt_task = asyncio.create_task(agent.prompt("start"))
        await model.blocking.wait()

        agent.abort()
        agent.abort()
        answer = await prompt_task

        assert answer.content == "partial"
        assert answer.stop_reason == "aborted"
        assert answer.tool_calls == []
        assert answer.metadata["aborted"] is True
        assert agent.last_run is not None
        assert agent.last_run.end_reason == "aborted"
        assert observed_signal == [True]
        event_types = [event.type for event in events]
        assert event_types.index("abort_requested") < event_types.index("message_end", 4)
        assert event_types[-1] == "agent_end"
        assert events[-1].data["reason"] == "aborted"
        assert agent.is_running is False

    asyncio.run(scenario())


def test_abort_discards_incomplete_tool_call_but_preserves_raw_fragments() -> None:
    class PartialToolModel:
        name = "partial-tool"

        def __init__(self) -> None:
            self.blocking = asyncio.Event()

        async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
            yield ToolCallDelta(
                index=0,
                tool_call_id="partial-1",
                tool_name="write_file",
                arguments_delta='{"path":"unfinished',
            )
            self.blocking.set()
            await asyncio.Event().wait()

    async def scenario() -> None:
        model = PartialToolModel()
        agent = Agent(model=model)
        task = asyncio.create_task(agent.prompt("start"))
        await model.blocking.wait()

        agent.abort()
        answer = await task

        assert answer.stop_reason == "aborted"
        assert answer.tool_calls == []
        assert answer.metadata["partial_tool_calls"] == [
            {
                "index": 0,
                "id": "partial-1",
                "name": "write_file",
                "arguments": '{"path":"unfinished',
            }
        ]

    asyncio.run(scenario())


def test_abort_finishes_current_tool_and_marks_unstarted_siblings_skipped() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        before_calls: list[str] = []
        after_calls: list[str] = []
        events: list[CoreEvent] = []

        async def work() -> str:
            started.set()
            await asyncio.Event().wait()
            return "unreachable"

        def before(context, assistant, call, *, signal=None) -> None:
            before_calls.append(call.id)

        def after(
            context,
            assistant,
            call,
            result,
            *,
            signal=None,
        ) -> ToolResult:
            after_calls.append(call.id)
            return ToolResult(content=result.content, metadata=dict(result.metadata))

        tool = Tool(
            name="work",
            description="block",
            parameters={"type": "object", "properties": {}},
            handler=work,
        )
        agent = Agent(
            model=ToolBatchModel(),
            tools=[tool],
            before_tool_call=before,
            after_tool_call=after,
        )
        agent.subscribe(events.append)
        task = asyncio.create_task(agent.prompt("work"))
        await started.wait()

        agent.abort()
        answer = await task

        assert answer.stop_reason == "tool_use"
        assert agent.last_run is not None
        assert agent.last_run.end_reason == "aborted"
        assert before_calls == ["call-1"]
        assert after_calls == ["call-1", "call-2"]
        results = [
            message for message in agent.messages if isinstance(message, ToolResultMessage)
        ]
        assert len(results) == 2
        assert all(result.is_error for result in results)
        assert results[0].metadata["aborted"] is True
        assert results[1].metadata == {"aborted": True, "skipped": True}
        ended_ids = [
            event.data["tool_call_id"]
            for event in events
            if event.type == "tool_execution_end"
        ]
        assert ended_ids == ["call-1", "call-2"]

    asyncio.run(scenario())


def test_abort_does_not_cancel_an_already_entered_hook() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        hook_finished = False
        executed = False

        async def before(context, assistant, call, *, signal=None) -> None:
            nonlocal hook_finished
            entered.set()
            await release.wait()
            hook_finished = True

        def work() -> str:
            nonlocal executed
            executed = True
            return "done"

        model = ToolBatchModel()
        model.stream = _single_tool_stream  # type: ignore[method-assign]
        agent = Agent(
            model=model,
            tools=[
                Tool(
                    name="work",
                    description="work",
                    parameters={"type": "object", "properties": {}},
                    handler=work,
                )
            ],
            before_tool_call=before,
        )
        task = asyncio.create_task(agent.prompt("work"))
        await entered.wait()

        agent.abort()
        release.set()
        await task

        assert hook_finished is True
        assert executed is False
        assert agent.last_run is not None
        assert agent.last_run.end_reason == "aborted"

    asyncio.run(scenario())


async def _single_tool_stream(
    context: AgentContext,
) -> AsyncIterator[ModelStreamEvent]:
    yield ModelComplete(
        message=AssistantMessage(
            content="",
            tool_calls=[ToolCall(id="call-1", name="work", arguments={})],
            stop_reason="tool_use",
        )
    )


def test_external_task_cancellation_cleans_up_then_reraises() -> None:
    async def scenario() -> None:
        model = BlockingTextModel()
        agent = Agent(model=model)
        task = asyncio.create_task(agent.prompt("start"))
        await model.blocking.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await agent.wait_for_idle()

        assert agent.last_run is not None
        assert agent.last_run.end_reason == "aborted"
        assert agent.is_running is False

    asyncio.run(scenario())


def test_external_cancellation_during_start_listener_still_commits_agent_end() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        events: list[CoreEvent] = []
        model = BlockingTextModel()
        agent = Agent(model=model)

        async def listener(event: CoreEvent) -> None:
            events.append(event)
            if event.type == "agent_start":
                entered.set()
                await release.wait()

        agent.subscribe(listener)
        task = asyncio.create_task(agent.prompt("start"))
        await entered.wait()

        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert agent.last_run is not None
        assert agent.last_run.end_reason == "aborted"
        assert [event.type for event in events].count("abort_requested") == 1
        assert events[-1].type == "agent_end"

    asyncio.run(scenario())


def test_sync_tool_completes_after_thread_safe_abort() -> None:
    started = Event()
    release = Event()
    agent: Agent

    def work() -> str:
        started.set()
        assert release.wait(timeout=5)
        return "late result"

    def request_abort() -> None:
        assert started.wait(timeout=5)
        agent.abort()
        release.set()

    model = ToolBatchModel()
    model.stream = _single_tool_stream  # type: ignore[method-assign]
    agent = Agent(
        model=model,
        tools=[
            Tool(
                name="work",
                description="work",
                parameters={"type": "object", "properties": {}},
                handler=work,
            )
        ],
    )
    abort_thread = Thread(target=request_abort)
    abort_thread.start()

    asyncio.run(agent.prompt("work"))
    abort_thread.join(timeout=5)

    result = next(
        message for message in agent.messages if isinstance(message, ToolResultMessage)
    )
    assert result.content == "late result"
    assert result.is_error is True
    assert result.metadata["completed_after_abort"] is True
    assert agent.last_run is not None
    assert agent.last_run.end_reason == "aborted"


def test_agent_can_run_again_after_abort_and_idle_abort_is_noop() -> None:
    class ReusableModel:
        name = "reusable"

        def __init__(self) -> None:
            self.calls = 0
            self.blocking = asyncio.Event()

        async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
            self.calls += 1
            if self.calls == 1:
                self.blocking.set()
                await asyncio.Event().wait()
            yield ModelComplete(
                message=AssistantMessage(content="second run", stop_reason="stop")
            )

    async def scenario() -> None:
        model = ReusableModel()
        agent = Agent(model=model)
        agent.abort()
        first = asyncio.create_task(agent.prompt("first"))
        await model.blocking.wait()
        agent.abort()
        await first

        answer = await agent.prompt("second")
        agent.abort()

        assert answer.content == "second run"
        assert agent.last_run is not None
        assert agent.last_run.end_reason == "completed"
        assert agent.is_running is False

    asyncio.run(scenario())


def test_abort_terminates_running_shell_process_tree(tmp_path) -> None:
    marker = tmp_path / "shell-started.txt"
    code = (
        "from pathlib import Path; "
        f"Path({str(marker)!r}).write_text('started'); "
        "import time; time.sleep(30)"
    )
    command = (
        subprocess.list2cmdline([sys.executable, "-c", code])
        if os.name == "nt"
        else shlex.join([sys.executable, "-c", code])
    )

    class ShellModel:
        name = "shell"

        async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
            yield ModelComplete(
                message=AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="shell-1",
                            name="shell_command",
                            arguments={"command": command},
                        )
                    ],
                    stop_reason="tool_use",
                )
            )

    async def scenario() -> None:
        agent = Agent(
            model=ShellModel(),
            tools=[
                create_shell_command_tool(
                    tmp_path,
                    timeout=60,
                    abort_grace_period=0.1,
                )
            ],
        )
        task = asyncio.create_task(agent.prompt("run shell"))
        for _ in range(200):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists()

        agent.abort()
        await asyncio.wait_for(task, timeout=5)

        result = next(
            message for message in agent.messages if isinstance(message, ToolResultMessage)
        )
        assert result.is_error is True
        assert result.metadata["aborted"] is True
        assert agent.last_run is not None
        assert agent.last_run.end_reason == "aborted"

    asyncio.run(scenario())
