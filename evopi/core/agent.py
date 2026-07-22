"""User-facing shell around the Core loop and transcript."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from uuid import uuid4

from evopi.core.agent_loop import (
    AfterModelCall,
    AfterToolCall,
    AfterTurn,
    AgentLoop,
    BeforeToolCall,
    PrepareContext,
    ShouldStopAfterTurn,
    TurnLimitError,
)
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent, EventListener
from evopi.core.messages import AssistantMessage, Message, SystemMessage, UserMessage
from evopi.core.model import Model
from evopi.core.run import AgentEndReason, AgentRunState
from evopi.core.tool import Tool


class Agent:
    def __init__(
        self,
        *,
        model: Model,
        system_prompt: str = "",
        tools: list[Tool] | None = None,
        max_turns: int = 20,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
        prepare_context: PrepareContext | None = None,
        after_model_call: AfterModelCall | None = None,
        after_turn: AfterTurn | None = None,
        should_stop_after_turn: ShouldStopAfterTurn | None = None,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.tools = list(tools or [])
        self.messages: list[Message] = []
        if system_prompt:
            self.messages.append(SystemMessage(content=system_prompt))
        self._loop = AgentLoop(max_turns=max_turns)
        self._listeners: list[EventListener] = []
        self._before_tool_call = before_tool_call
        self._after_tool_call = after_tool_call
        self._prepare_context = prepare_context
        self._after_model_call = after_model_call
        self._after_turn = after_turn
        self._should_stop_after_turn = should_stop_after_turn
        self._run_lock = asyncio.Lock()
        self._current_run_id: str | None = None
        self._last_run: AgentRunState | None = None

    @property
    def last_run(self) -> AgentRunState | None:
        return self._last_run

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def prompt(self, content: str) -> AssistantMessage:
        if not content:
            raise ValueError("Prompt content cannot be empty")
        if self._run_lock.locked():
            raise RuntimeError("Agent is already running")

        async with self._run_lock:
            run_id = uuid4().hex
            self._current_run_id = run_id
            run_start = len(self.messages)
            user_message = UserMessage(content=content)
            self.messages.append(user_message)
            await self._emit(CoreEvent(type="agent_start", run_id=run_id))
            await self._emit(
                CoreEvent(
                    type="message_start",
                    run_id=run_id,
                    data={
                        "message_id": user_message.id,
                        "role": user_message.role,
                    },
                )
            )
            await self._emit(
                CoreEvent(type="message_end", run_id=run_id, data={"message": user_message})
            )
            try:
                result = await self._loop.run_with_result(
                    model=self.model,
                    context=AgentContext(messages=self.messages, tools=self.tools),
                    emit=self._emit,
                    before_tool_call=self._before_tool_call,
                    after_tool_call=self._after_tool_call,
                    prepare_context=self._prepare_context,
                    after_model_call=self._after_model_call,
                    after_turn=self._after_turn,
                    should_stop_after_turn=self._should_stop_after_turn,
                    run_id=run_id,
                )
            except Exception as exc:
                reason: AgentEndReason = (
                    "turn_limit" if isinstance(exc, TurnLimitError) else "error"
                )
                error = f"{type(exc).__name__}: {exc}"
                self._last_run = AgentRunState(
                    run_id=run_id,
                    end_reason=reason,
                    error=error,
                )
                await self._emit(
                    CoreEvent(
                        type="error",
                        run_id=run_id,
                        data={"error": error},
                    )
                )
                await self._emit(
                    CoreEvent(
                        type="agent_end",
                        run_id=run_id,
                        data={
                            "reason": reason,
                            "messages": list(self.messages[run_start:]),
                            "error": error,
                        },
                    )
                )
                self._current_run_id = None
                raise
            self._last_run = AgentRunState(
                run_id=run_id,
                end_reason=result.end_reason,
            )
            await self._emit(
                CoreEvent(
                    type="agent_end",
                    run_id=run_id,
                    data={
                        "reason": result.end_reason,
                        "messages": list(self.messages[run_start:]),
                        "error": None,
                    },
                )
            )
            self._current_run_id = None
            return result.message

    def reset(self) -> None:
        if self._run_lock.locked():
            raise RuntimeError("Cannot reset a running agent")
        self.messages.clear()
        self._last_run = None
        if self.system_prompt:
            self.messages.append(SystemMessage(content=self.system_prompt))

    async def _emit(self, event: CoreEvent) -> None:
        if event.run_id is None:
            event.run_id = self._current_run_id
        for listener in tuple(self._listeners):
            value = listener(event)
            if inspect.isawaitable(value):
                await value

    async def emit_event(self, event: CoreEvent) -> None:
        """Emit an event produced by an injected Harness hook."""

        await self._emit(event)


__all__ = ["Agent"]
