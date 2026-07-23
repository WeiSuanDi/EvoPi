"""The stable model → tool → result execution loop."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeAlias, cast
from uuid import uuid4

from evopi.core.cancellation import AbortSignal, call_with_optional_signal
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent, EventListener, notify
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.model import Model
from evopi.core.run import AgentLoopResult
from evopi.core.stream import (
    AssistantMessageBuilder,
    ModelComplete,
    ModelStreamEvent,
    TextDelta,
    ToolCallDelta,
)
from evopi.core.tool import ToolCall, ToolResult
from evopi.core.types import JsonObject


class AgentLoopError(RuntimeError):
    pass


class ModelProtocolError(AgentLoopError):
    pass


class TurnLimitError(AgentLoopError):
    pass


@dataclass(slots=True, kw_only=True)
class BeforeToolCallResult:
    block: bool = False
    reason: str | None = None
    arguments: JsonObject | None = None


BeforeToolCall: TypeAlias = Callable[
    ...,
    Awaitable[BeforeToolCallResult | None] | BeforeToolCallResult | None,
]
AfterToolCall: TypeAlias = Callable[
    ...,
    Awaitable[ToolResult | None] | ToolResult | None,
]
PrepareContext: TypeAlias = Callable[
    ...,
    Awaitable[AgentContext | None] | AgentContext | None,
]
AfterModelCall: TypeAlias = Callable[
    ...,
    Awaitable[AssistantMessage | None] | AssistantMessage | None,
]
AfterTurn: TypeAlias = Callable[..., Awaitable[None] | None]
ShouldStopAfterTurn: TypeAlias = Callable[..., Awaitable[bool] | bool]

_MODEL_ABORTED = object()


class AgentLoop:
    def __init__(self, *, max_turns: int = 20) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        self.max_turns = max_turns

    async def run(
        self,
        *,
        model: Model,
        context: AgentContext,
        emit: EventListener | None = None,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
        prepare_context: PrepareContext | None = None,
        after_model_call: AfterModelCall | None = None,
        after_turn: AfterTurn | None = None,
        should_stop_after_turn: ShouldStopAfterTurn | None = None,
        run_id: str | None = None,
        signal: AbortSignal | None = None,
    ) -> AssistantMessage:
        result = await self.run_with_result(
            model=model,
            context=context,
            emit=emit,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            prepare_context=prepare_context,
            after_model_call=after_model_call,
            after_turn=after_turn,
            should_stop_after_turn=should_stop_after_turn,
            run_id=run_id,
            signal=signal,
        )
        return result.message

    async def run_with_result(
        self,
        *,
        model: Model,
        context: AgentContext,
        emit: EventListener | None = None,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
        prepare_context: PrepareContext | None = None,
        after_model_call: AfterModelCall | None = None,
        after_turn: AfterTurn | None = None,
        should_stop_after_turn: ShouldStopAfterTurn | None = None,
        run_id: str | None = None,
        signal: AbortSignal | None = None,
    ) -> AgentLoopResult:
        for turn in range(1, self.max_turns + 1):
            await notify(
                emit,
                CoreEvent(type="turn_start", run_id=run_id, data={"turn": turn}),
                signal=signal,
            )
            await notify(
                emit,
                CoreEvent(
                    type="model_start",
                    run_id=run_id,
                    data={"turn": turn, "model": model.name},
                ),
                signal=signal,
            )

            assistant = await self._consume_model(
                model,
                context,
                emit,
                run_id,
                prepare_context=prepare_context,
                signal=signal,
            )
            model_was_aborted = assistant.stop_reason == "aborted"
            if after_model_call is not None:
                replacement = call_with_optional_signal(
                    after_model_call,
                    context,
                    assistant,
                    signal=signal,
                )
                if inspect.isawaitable(replacement):
                    replacement = await replacement
                if replacement is not None:
                    replacement.id = assistant.id
                    if model_was_aborted:
                        replacement.stop_reason = "aborted"
                        replacement.tool_calls = []
                        replacement.metadata = {
                            **replacement.metadata,
                            **assistant.metadata,
                            "aborted": True,
                        }
                    assistant = replacement
            context.append(assistant)
            await notify(
                emit,
                CoreEvent(type="message_end", run_id=run_id, data={"message": assistant}),
                signal=signal,
            )

            if not assistant.tool_calls:
                await notify(
                    emit,
                    CoreEvent(
                        type="turn_end",
                        run_id=run_id,
                        data={
                            "turn": turn,
                            "message": assistant,
                            "tool_results": [],
                        },
                    ),
                    signal=signal,
                )
                should_stop = await self._finish_turn(
                    context=context,
                    assistant=assistant,
                    results=[],
                    after_turn=after_turn,
                    should_stop_after_turn=should_stop_after_turn,
                    signal=signal,
                )
                if signal is not None and signal.aborted:
                    await signal._wait_until_notified()
                    return AgentLoopResult(message=assistant, end_reason="aborted")
                return AgentLoopResult(
                    message=assistant,
                    end_reason="terminated" if should_stop else "completed",
                )

            tool_messages: list[ToolResultMessage] = []
            for tool_call in assistant.tool_calls:
                result = await self._execute_tool_call(
                    context=context,
                    assistant=assistant,
                    tool_call=tool_call,
                    emit=emit,
                    before_tool_call=before_tool_call,
                    after_tool_call=after_tool_call,
                    run_id=run_id,
                    signal=signal,
                )
                message = ToolResultMessage(
                    content=result.content,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    is_error=result.is_error,
                    terminate=result.terminate,
                    metadata=result.metadata,
                )
                await notify(
                    emit,
                    CoreEvent(
                        type="message_start",
                        run_id=run_id,
                        data={"message_id": message.id, "role": message.role},
                    ),
                    signal=signal,
                )
                context.append(message)
                tool_messages.append(message)
                await notify(
                    emit,
                    CoreEvent(type="message_end", run_id=run_id, data={"message": message}),
                    signal=signal,
                )

            terminate = bool(tool_messages) and all(
                message.terminate for message in tool_messages
            )

            await notify(
                emit,
                CoreEvent(
                    type="turn_end",
                    run_id=run_id,
                    data={
                        "turn": turn,
                        "message": assistant,
                        "tool_results": tool_messages,
                    },
                ),
                signal=signal,
            )
            should_stop = await self._finish_turn(
                context=context,
                assistant=assistant,
                results=tool_messages,
                after_turn=after_turn,
                should_stop_after_turn=should_stop_after_turn,
                signal=signal,
            )
            if signal is not None and signal.aborted:
                await signal._wait_until_notified()
                return AgentLoopResult(message=assistant, end_reason="aborted")
            if terminate or should_stop:
                return AgentLoopResult(message=assistant, end_reason="terminated")

        raise TurnLimitError(f"Agent loop exceeded {self.max_turns} turns")

    @staticmethod
    async def _finish_turn(
        *,
        context: AgentContext,
        assistant: AssistantMessage,
        results: list[ToolResultMessage],
        after_turn: AfterTurn | None,
        should_stop_after_turn: ShouldStopAfterTurn | None,
        signal: AbortSignal | None,
    ) -> bool:
        if after_turn is not None:
            value = call_with_optional_signal(
                after_turn,
                context,
                assistant,
                results,
                signal=signal,
            )
            if inspect.isawaitable(value):
                await value
        if should_stop_after_turn is None:
            return False
        should_stop = call_with_optional_signal(
            should_stop_after_turn,
            context,
            assistant,
            results,
            signal=signal,
        )
        if inspect.isawaitable(should_stop):
            should_stop = await should_stop
        if not isinstance(should_stop, bool):
            raise TypeError("should_stop_after_turn must return bool")
        return should_stop

    async def _consume_model(
        self,
        model: Model,
        context: AgentContext,
        emit: EventListener | None,
        run_id: str | None,
        prepare_context: PrepareContext | None,
        signal: AbortSignal | None,
    ) -> AssistantMessage:
        complete: AssistantMessage | None = None
        message_id = uuid4().hex
        builder = AssistantMessageBuilder()
        partial_calls: dict[int, dict[str, Any]] = {}
        model_context = context.snapshot()
        await notify(
            emit,
            CoreEvent(
                type="message_start",
                run_id=run_id,
                data={"message_id": message_id, "role": "assistant"},
            ),
            signal=signal,
        )
        if prepare_context is not None:
            replacement = call_with_optional_signal(
                prepare_context,
                model_context,
                signal=signal,
            )
            if inspect.isawaitable(replacement):
                replacement = await replacement
            if replacement is not None:
                model_context = replacement

        if signal is not None and signal.aborted:
            await signal._wait_until_notified()
            return self._build_aborted_message(builder, partial_calls, message_id)

        stream = call_with_optional_signal(model.stream, model_context, signal=signal)
        iterator = stream.__aiter__()
        aborted = False
        try:
            while True:
                try:
                    event = await self._next_model_event(iterator, signal)
                except StopAsyncIteration:
                    break
                if event is _MODEL_ABORTED:
                    aborted = True
                    break
                model_event = cast(ModelStreamEvent, event)
                if isinstance(model_event, TextDelta):
                    builder.add_text(model_event.delta)
                    await notify(
                        emit,
                        CoreEvent(
                            type="message_update",
                            run_id=run_id,
                            data={
                                "message_id": message_id,
                                "role": "assistant",
                                "kind": "text",
                                "delta": model_event.delta,
                            },
                        ),
                        signal=signal,
                    )
                elif isinstance(model_event, ToolCallDelta):
                    builder.add_tool_call_delta(
                        index=model_event.index,
                        arguments_delta=model_event.arguments_delta,
                        tool_call_id=model_event.tool_call_id,
                        tool_name=model_event.tool_name,
                    )
                    state = partial_calls.setdefault(
                        model_event.index,
                        {"index": model_event.index, "id": "", "name": "", "arguments": ""},
                    )
                    if model_event.tool_call_id:
                        state["id"] = model_event.tool_call_id
                    if model_event.tool_name:
                        state["name"] += model_event.tool_name
                    state["arguments"] += model_event.arguments_delta
                    await notify(
                        emit,
                        CoreEvent(
                            type="message_update",
                            run_id=run_id,
                            data={
                                "message_id": message_id,
                                "role": "assistant",
                                "kind": "tool_call",
                                "index": model_event.index,
                                "delta": model_event.arguments_delta,
                                "tool_call_id": model_event.tool_call_id,
                                "tool_name": model_event.tool_name,
                            },
                        ),
                        signal=signal,
                    )
                elif isinstance(model_event, ModelComplete):
                    if complete is not None:
                        raise ModelProtocolError("Model emitted more than one completion")
                    complete = model_event.message
                else:  # pragma: no cover - protects third-party Model implementations.
                    raise ModelProtocolError(f"Unknown model stream event: {model_event!r}")
        finally:
            if aborted:
                await self._close_model_iterator(iterator)

        if aborted or (signal is not None and signal.aborted and complete is None):
            if signal is not None:
                await signal._wait_until_notified()
            return self._build_aborted_message(builder, partial_calls, message_id)
        if complete is None:
            raise ModelProtocolError("Model stream ended without a completion")
        complete.id = message_id
        return complete

    @staticmethod
    async def _next_model_event(
        iterator: Any,
        signal: AbortSignal | None,
    ) -> ModelStreamEvent | object:
        if signal is None:
            return await anext(iterator)
        if signal.aborted:
            return _MODEL_ABORTED

        next_task = asyncio.create_task(anext(iterator))
        abort_task = asyncio.create_task(signal.wait())
        done, _ = await asyncio.wait(
            {next_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if abort_task in done:
            if not next_task.done():
                next_task.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                await next_task
            return _MODEL_ABORTED

        abort_task.cancel()
        with suppress(asyncio.CancelledError):
            await abort_task
        return await next_task

    @staticmethod
    async def _close_model_iterator(iterator: Any) -> None:
        close = getattr(iterator, "aclose", None)
        if close is None:
            return
        with suppress(asyncio.CancelledError, RuntimeError):
            value = close()
            if inspect.isawaitable(value):
                await value

    @staticmethod
    def _build_aborted_message(
        builder: AssistantMessageBuilder,
        partial_calls: dict[int, dict[str, Any]],
        message_id: str,
    ) -> AssistantMessage:
        message = builder.build(
            stop_reason="aborted",
            metadata={
                "aborted": True,
                "partial_tool_calls": [partial_calls[index] for index in sorted(partial_calls)],
            },
        )
        message.id = message_id
        message.tool_calls = []
        return message

    async def _execute_tool_call(
        self,
        *,
        context: AgentContext,
        assistant: AssistantMessage,
        tool_call: ToolCall,
        emit: EventListener | None,
        before_tool_call: BeforeToolCall | None,
        after_tool_call: AfterToolCall | None,
        run_id: str | None,
        signal: AbortSignal | None,
    ) -> ToolResult:
        await notify(
            emit,
            CoreEvent(
                type="tool_execution_start",
                run_id=run_id,
                data={
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.name,
                    "args": tool_call.arguments,
                },
            ),
            signal=signal,
        )

        skipped = signal is not None and signal.aborted
        if skipped:
            assert signal is not None
            await signal._wait_until_notified()
            result = ToolResult(
                content="Tool skipped because the run was aborted",
                is_error=True,
                metadata={"aborted": True, "skipped": True},
            )
        else:
            result = await self._prepare_and_execute_tool(
                context=context,
                assistant=assistant,
                tool_call=tool_call,
                before_tool_call=before_tool_call,
                signal=signal,
            )

        authoritative_metadata = {
            key: value
            for key, value in result.metadata.items()
            if key in {"aborted", "skipped", "completed_after_abort"}
        }
        if after_tool_call is not None and (skipped or not result.metadata.get("blocked")):
            replacement = call_with_optional_signal(
                after_tool_call,
                context,
                assistant,
                tool_call,
                result,
                signal=signal,
            )
            if inspect.isawaitable(replacement):
                replacement = await replacement
            if replacement is not None:
                result = replacement

        if signal is not None and signal.aborted:
            await signal._wait_until_notified()
            result = self._force_aborted_result(
                result,
                skipped=skipped,
                authoritative_metadata=authoritative_metadata,
            )

        await self._emit_tool_execution_end(
            emit=emit,
            run_id=run_id,
            tool_call=tool_call,
            result=result,
            signal=signal,
        )
        return result

    @staticmethod
    async def _prepare_and_execute_tool(
        *,
        context: AgentContext,
        assistant: AssistantMessage,
        tool_call: ToolCall,
        before_tool_call: BeforeToolCall | None,
        signal: AbortSignal | None,
    ) -> ToolResult:
        arguments = dict(tool_call.arguments)
        if before_tool_call is not None:
            decision = call_with_optional_signal(
                before_tool_call,
                context,
                assistant,
                tool_call,
                signal=signal,
            )
            if inspect.isawaitable(decision):
                decision = await decision
            if signal is not None and signal.aborted:
                return ToolResult(
                    content="Operation aborted before tool execution",
                    is_error=True,
                    metadata={"aborted": True},
                )
            if decision is not None:
                if decision.block:
                    return ToolResult(
                        content=decision.reason or "Tool execution was blocked",
                        is_error=True,
                        metadata={"blocked": True},
                    )
                if decision.arguments is not None:
                    arguments = decision.arguments

        tool = next((item for item in context.tools if item.name == tool_call.name), None)
        if tool is None:
            return ToolResult(content=f"Tool '{tool_call.name}' not found", is_error=True)
        return await tool.execute(arguments, signal=signal)

    @staticmethod
    def _force_aborted_result(
        result: ToolResult,
        *,
        skipped: bool,
        authoritative_metadata: dict[str, object],
    ) -> ToolResult:
        metadata = {**result.metadata, **authoritative_metadata, "aborted": True}
        if skipped:
            metadata["skipped"] = True
        return ToolResult(
            content=result.content,
            is_error=True,
            terminate=False,
            metadata=metadata,
        )

    @staticmethod
    async def _emit_tool_execution_end(
        *,
        emit: EventListener | None,
        run_id: str | None,
        tool_call: ToolCall,
        result: ToolResult,
        signal: AbortSignal | None,
    ) -> None:
        await notify(
            emit,
            CoreEvent(
                type="tool_execution_end",
                run_id=run_id,
                data={
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.name,
                    "result": result,
                    "is_error": result.is_error,
                },
            ),
            signal=signal,
        )


__all__ = [
    "AfterModelCall",
    "AfterTurn",
    "AfterToolCall",
    "AgentLoop",
    "AgentLoopError",
    "BeforeToolCall",
    "BeforeToolCallResult",
    "ModelProtocolError",
    "PrepareContext",
    "ShouldStopAfterTurn",
    "TurnLimitError",
]
