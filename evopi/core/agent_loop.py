"""The stable model → tool → result execution loop."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeAlias
from uuid import uuid4

from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent, EventListener, notify
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.model import Model
from evopi.core.run import AgentLoopResult
from evopi.core.stream import ModelComplete, TextDelta, ToolCallDelta
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
    [AgentContext, AssistantMessage, ToolCall],
    Awaitable[BeforeToolCallResult | None] | BeforeToolCallResult | None,
]
AfterToolCall: TypeAlias = Callable[
    [AgentContext, AssistantMessage, ToolCall, ToolResult],
    Awaitable[ToolResult | None] | ToolResult | None,
]
PrepareContext: TypeAlias = Callable[
    [AgentContext], Awaitable[AgentContext | None] | AgentContext | None
]
AfterModelCall: TypeAlias = Callable[
    [AgentContext, AssistantMessage],
    Awaitable[AssistantMessage | None] | AssistantMessage | None,
]
AfterTurn: TypeAlias = Callable[
    [AgentContext, AssistantMessage, list[ToolResultMessage]], Awaitable[None] | None
]
ShouldStopAfterTurn: TypeAlias = Callable[
    [AgentContext, AssistantMessage, list[ToolResultMessage]], Awaitable[bool] | bool
]


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
    ) -> AgentLoopResult:
        for turn in range(1, self.max_turns + 1):
            await notify(emit, CoreEvent(type="turn_start", run_id=run_id, data={"turn": turn}))
            await notify(
                emit,
                CoreEvent(
                    type="model_start",
                    run_id=run_id,
                    data={"turn": turn, "model": model.name},
                ),
            )

            assistant = await self._consume_model(
                model, context, emit, run_id, prepare_context=prepare_context
            )
            if after_model_call is not None:
                replacement = after_model_call(context, assistant)
                if inspect.isawaitable(replacement):
                    replacement = await replacement
                if replacement is not None:
                    replacement.id = assistant.id
                    assistant = replacement
            context.append(assistant)
            await notify(
                emit,
                CoreEvent(type="message_end", run_id=run_id, data={"message": assistant}),
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
                )
                should_stop = await self._finish_turn(
                    context=context,
                    assistant=assistant,
                    results=[],
                    after_turn=after_turn,
                    should_stop_after_turn=should_stop_after_turn,
                )
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
                        data={
                            "message_id": message.id,
                            "role": message.role,
                        },
                    ),
                )
                context.append(message)
                tool_messages.append(message)
                await notify(
                    emit,
                    CoreEvent(type="message_end", run_id=run_id, data={"message": message}),
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
            )
            should_stop = await self._finish_turn(
                context=context,
                assistant=assistant,
                results=tool_messages,
                after_turn=after_turn,
                should_stop_after_turn=should_stop_after_turn,
            )
            if terminate or should_stop:
                return AgentLoopResult(
                    message=assistant,
                    end_reason="terminated",
                )

        raise TurnLimitError(f"Agent loop exceeded {self.max_turns} turns")

    @staticmethod
    async def _finish_turn(
        *,
        context: AgentContext,
        assistant: AssistantMessage,
        results: list[ToolResultMessage],
        after_turn: AfterTurn | None,
        should_stop_after_turn: ShouldStopAfterTurn | None,
    ) -> bool:
        if after_turn is not None:
            value = after_turn(context, assistant, results)
            if inspect.isawaitable(value):
                await value
        if should_stop_after_turn is None:
            return False
        should_stop = should_stop_after_turn(context, assistant, results)
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
    ) -> AssistantMessage:
        complete: AssistantMessage | None = None
        message_id = uuid4().hex
        model_context = context.snapshot()
        if prepare_context is not None:
            replacement = prepare_context(model_context)
            if inspect.isawaitable(replacement):
                replacement = await replacement
            if replacement is not None:
                model_context = replacement
        await notify(
            emit,
            CoreEvent(
                type="message_start",
                run_id=run_id,
                data={"message_id": message_id, "role": "assistant"},
            ),
        )
        async for event in model.stream(model_context):
            if isinstance(event, TextDelta):
                await notify(
                    emit,
                    CoreEvent(
                        type="message_update",
                        run_id=run_id,
                        data={
                            "message_id": message_id,
                            "role": "assistant",
                            "kind": "text",
                            "delta": event.delta,
                        },
                    ),
                )
            elif isinstance(event, ToolCallDelta):
                await notify(
                    emit,
                    CoreEvent(
                        type="message_update",
                        run_id=run_id,
                        data={
                            "message_id": message_id,
                            "role": "assistant",
                            "kind": "tool_call",
                            "index": event.index,
                            "delta": event.arguments_delta,
                            "tool_call_id": event.tool_call_id,
                            "tool_name": event.tool_name,
                        },
                    ),
                )
            elif isinstance(event, ModelComplete):
                if complete is not None:
                    raise ModelProtocolError("Model emitted more than one completion")
                complete = event.message
            else:  # pragma: no cover - protects third-party Model implementations.
                raise ModelProtocolError(f"Unknown model stream event: {event!r}")

        if complete is None:
            raise ModelProtocolError("Model stream ended without a completion")
        complete.id = message_id
        return complete

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
        )
        arguments = dict(tool_call.arguments)
        if before_tool_call is not None:
            decision = before_tool_call(context, assistant, tool_call)
            if inspect.isawaitable(decision):
                decision = await decision
            if decision is not None:
                if decision.block:
                    result = ToolResult(
                        content=decision.reason or "Tool execution was blocked",
                        is_error=True,
                        metadata={"blocked": True},
                    )
                    await self._emit_tool_execution_end(
                        emit=emit,
                        run_id=run_id,
                        tool_call=tool_call,
                        result=result,
                    )
                    return result
                if decision.arguments is not None:
                    arguments = decision.arguments

        tool = next((item for item in context.tools if item.name == tool_call.name), None)
        if tool is None:
            result = ToolResult(content=f"Tool '{tool_call.name}' not found", is_error=True)
        else:
            result = await tool.execute(arguments)

        if after_tool_call is not None:
            replacement = after_tool_call(context, assistant, tool_call, result)
            if inspect.isawaitable(replacement):
                replacement = await replacement
            if replacement is not None:
                result = replacement
        await self._emit_tool_execution_end(
            emit=emit,
            run_id=run_id,
            tool_call=tool_call,
            result=result,
        )
        return result

    @staticmethod
    async def _emit_tool_execution_end(
        *,
        emit: EventListener | None,
        run_id: str | None,
        tool_call: ToolCall,
        result: ToolResult,
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
