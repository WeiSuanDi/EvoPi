"""User-facing shell around the Core loop and transcript."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
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
from evopi.core.cancellation import (
    AbortController,
    AbortSignal,
    call_with_optional_signal,
)
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent, EventListener
from evopi.core.messages import AssistantMessage, Message, SystemMessage, UserMessage
from evopi.core.model import Model
from evopi.core.model_errors import ModelRetryConfig, error_info_from_exception
from evopi.core.run import AgentEndReason, AgentLoopResult, AgentRunState
from evopi.core.tool import Tool


@dataclass(slots=True)
class _ActiveRun:
    controller: AbortController
    idle: asyncio.Future[None]
    accepting_abort: bool = True
    monitor: asyncio.Task[None] | None = None
    monitor_error: str | None = None


class Agent:
    def __init__(
        self,
        *,
        model: Model,
        system_prompt: str = "",
        tools: list[Tool] | None = None,
        max_turns: int = 20,
        retry_config: ModelRetryConfig | None = None,
        deadline: float | None = None,
        tool_timeout: float | None = None,
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
        self._loop = AgentLoop(max_turns=max_turns, retry_config=retry_config, deadline=deadline, tool_timeout=tool_timeout)
        self._listeners: list[EventListener] = []
        self._before_tool_call = before_tool_call
        self._after_tool_call = after_tool_call
        self._prepare_context = prepare_context
        self._after_model_call = after_model_call
        self._after_turn = after_turn
        self._should_stop_after_turn = should_stop_after_turn
        self._run_lock = asyncio.Lock()
        self._active_guard = Lock()
        self._active_run: _ActiveRun | None = None
        self._current_run_id: str | None = None
        self._last_run: AgentRunState | None = None

    @property
    def last_run(self) -> AgentRunState | None:
        return self._last_run

    @property
    def signal(self) -> AbortSignal | None:
        with self._active_guard:
            active = self._active_run
            return active.controller.signal if active is not None else None

    @property
    def is_running(self) -> bool:
        with self._active_guard:
            return self._active_run is not None

    def abort(self) -> None:
        """Request cancellation of the active run, if any."""

        with self._active_guard:
            active = self._active_run
            if active is None or not active.accepting_abort:
                return
            active.controller.abort()

    async def wait_for_idle(self) -> None:
        with self._active_guard:
            active = self._active_run
            idle = active.idle if active is not None else None
        if idle is not None:
            await asyncio.shield(idle)

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
            running_loop = asyncio.get_running_loop()
            run_id = uuid4().hex
            active = _ActiveRun(
                controller=AbortController(loop=running_loop),
                idle=running_loop.create_future(),
            )
            with self._active_guard:
                self._active_run = active
            self._current_run_id = run_id
            run_start = len(self.messages)
            user_message = UserMessage(content=content)
            self.messages.append(user_message)
            caller_cancelled = False
            result: AgentLoopResult | None = None
            failure: Exception | None = None
            try:
                startup_task = asyncio.create_task(
                    self._emit_run_start(run_id, user_message)
                )
                try:
                    await asyncio.shield(startup_task)
                except asyncio.CancelledError:
                    caller_cancelled = True
                    active.controller.abort()
                    try:
                        await asyncio.shield(startup_task)
                    except asyncio.CancelledError:
                        startup_task.cancel()
                        raise
                    except Exception as exc:
                        failure = exc
                except Exception as exc:
                    failure = exc

                active.monitor = asyncio.create_task(self._monitor_abort(active, run_id))
                if failure is None:
                    loop_task = asyncio.create_task(
                        self._loop.run_with_result(
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
                            signal=active.controller.signal,
                        )
                    )
                    try:
                        result = await asyncio.shield(loop_task)
                    except asyncio.CancelledError:
                        caller_cancelled = True
                        active.controller.abort()
                        try:
                            result = await asyncio.shield(loop_task)
                        except asyncio.CancelledError:
                            loop_task.cancel()
                            raise
                    except Exception as exc:
                        failure = exc

                # Closing the acceptance window and observing the signal happen under the
                # same lock used by abort().  This is the run's commit point: an abort that
                # wins before it is authoritative; one arriving afterwards is a no-op.
                with self._active_guard:
                    active.accepting_abort = False
                await self._settle_abort_monitor(active)
                if active.monitor_error is not None:
                    if failure is None:
                        failure = RuntimeError(active.monitor_error)
                    else:
                        failure = RuntimeError(f"{failure}; {active.monitor_error}")

                if failure is not None and not active.controller.signal.aborted:
                    await self._finish_failed_run(
                        run_id=run_id,
                        run_start=run_start,
                        failure=failure,
                    )
                    raise failure

                if failure is not None:
                    await self._emit_cleanup_error(run_id, failure)

                if result is None:
                    result = AgentLoopResult(
                        message=self._last_assistant_message(run_start),
                        end_reason="aborted",
                    )
                reason: AgentEndReason = (
                    "aborted" if active.controller.signal.aborted else result.end_reason
                )
                error = (
                    f"{type(failure).__name__}: {failure}" if failure is not None else None
                )
                self._last_run = AgentRunState(
                    run_id=run_id,
                    end_reason=reason,
                    error=error,
                    error_info=(
                        error_info_from_exception(failure)
                        if failure is not None
                        else None
                    ),
                )
                await self._emit(
                    CoreEvent(
                        type="agent_end",
                        run_id=run_id,
                        data={
                            "reason": reason,
                            "messages": list(self.messages[run_start:]),
                            "error": error,
                            "error_info": (
                                error_info_from_exception(failure)
                                if failure is not None
                                else None
                            ),
                        },
                    )
                )
                if caller_cancelled:
                    raise asyncio.CancelledError
                return result.message
            finally:
                await self._stop_abort_monitor(active)
                self._current_run_id = None
                with self._active_guard:
                    if self._active_run is active:
                        self._active_run = None
                if not active.idle.done():
                    active.idle.set_result(None)

    async def _emit_run_start(self, run_id: str, user_message: UserMessage) -> None:
        await self._emit(CoreEvent(type="agent_start", run_id=run_id))
        await self._emit(
            CoreEvent(
                type="message_start",
                run_id=run_id,
                data={"message_id": user_message.id, "role": user_message.role},
            )
        )
        await self._emit(
            CoreEvent(
                type="message_end",
                run_id=run_id,
                data={"message": user_message},
            )
        )

    def reset(self) -> None:
        if self.is_running:
            raise RuntimeError("Cannot reset a running agent")
        self.messages.clear()
        self._last_run = None
        if self.system_prompt:
            self.messages.append(SystemMessage(content=self.system_prompt))

    async def _monitor_abort(self, active: _ActiveRun, run_id: str) -> None:
        signal = active.controller.signal
        await signal.wait()
        try:
            await self._emit(
                CoreEvent(
                    type="abort_requested",
                    run_id=run_id,
                    data={"requested_at": signal.requested_at},
                )
            )
        except Exception as exc:
            active.monitor_error = f"Abort event listener failed: {type(exc).__name__}: {exc}"
        finally:
            signal._mark_notified()

    @staticmethod
    async def _settle_abort_monitor(active: _ActiveRun) -> None:
        monitor = active.monitor
        if monitor is None:
            return
        if active.controller.signal.aborted:
            await monitor
            return
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _stop_abort_monitor(active: _ActiveRun) -> None:
        monitor = active.monitor
        if monitor is None or monitor.done():
            return
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass

    async def _finish_failed_run(
        self,
        *,
        run_id: str,
        run_start: int,
        failure: Exception,
    ) -> None:
        reason: AgentEndReason = (
            "turn_limit" if isinstance(failure, TurnLimitError) else "error"
        )
        error = f"{type(failure).__name__}: {failure}"
        error_info = error_info_from_exception(failure)
        self._last_run = AgentRunState(
            run_id=run_id,
            end_reason=reason,
            error=error,
            error_info=error_info,
        )
        await self._emit(
            CoreEvent(
                type="error",
                run_id=run_id,
                data={"error": error, "error_info": error_info},
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
                    "error_info": error_info,
                },
            )
        )

    async def _emit_cleanup_error(self, run_id: str, failure: Exception) -> None:
        error = f"{type(failure).__name__}: {failure}"
        await self._emit(
            CoreEvent(
                type="error",
                run_id=run_id,
                data={
                    "error": error,
                    "error_info": error_info_from_exception(failure),
                },
            )
        )

    def _last_assistant_message(self, run_start: int) -> AssistantMessage:
        for message in reversed(self.messages[run_start:]):
            if isinstance(message, AssistantMessage):
                return message
        message = AssistantMessage(
            content="",
            stop_reason="aborted",
            metadata={"aborted": True},
        )
        self.messages.append(message)
        return message

    async def _emit(self, event: CoreEvent) -> None:
        if event.run_id is None:
            event.run_id = self._current_run_id
        signal = self.signal
        for listener in tuple(self._listeners):
            value = call_with_optional_signal(listener, event, signal=signal)
            if inspect.isawaitable(value):
                await value

    async def emit_event(self, event: CoreEvent) -> None:
        """Emit an event produced by an injected Harness hook."""

        await self._emit(event)


__all__ = ["Agent"]
