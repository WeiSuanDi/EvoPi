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
)
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent, EventListener
from evopi.core.messages import AssistantMessage, Message, SystemMessage, UserMessage
from evopi.core.model import Model
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
        self._run_lock = asyncio.Lock()
        self._current_run_id: str | None = None

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
            user_message = UserMessage(content=content)
            self.messages.append(user_message)
            await self._emit(CoreEvent(type="agent_start", run_id=run_id))
            await self._emit(
                CoreEvent(type="user_message", run_id=run_id, data={"message": user_message})
            )
            try:
                answer = await self._loop.run(
                    model=self.model,
                    context=AgentContext(messages=self.messages, tools=self.tools),
                    emit=self._emit,
                    before_tool_call=self._before_tool_call,
                    after_tool_call=self._after_tool_call,
                    prepare_context=self._prepare_context,
                    after_model_call=self._after_model_call,
                    after_turn=self._after_turn,
                    run_id=run_id,
                )
            except Exception as exc:
                await self._emit(
                    CoreEvent(
                        type="error",
                        run_id=run_id,
                        data={"error": f"{type(exc).__name__}: {exc}"},
                    )
                )
                await self._emit(CoreEvent(type="agent_end", run_id=run_id, data={"ok": False}))
                self._current_run_id = None
                raise
            await self._emit(CoreEvent(type="agent_end", run_id=run_id, data={"ok": True}))
            self._current_run_id = None
            return answer

    def reset(self) -> None:
        if self._run_lock.locked():
            raise RuntimeError("Cannot reset a running agent")
        self.messages.clear()
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
