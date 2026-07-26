"""Provider-neutral model attempt and retry execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from evopi.core.cancellation import AbortSignal
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent, EventListener, notify
from evopi.core.messages import AssistantMessage
from evopi.core.model import Model
from evopi.core.model_errors import (
    ModelErrorInfo,
    ModelRetryConfig,
    error_info_from_exception,
)

ConsumeModelAttempt = Callable[..., Awaitable[AssistantMessage]]
RetryWait = Callable[
    [float, AbortSignal | None, asyncio.Event | None],
    Awaitable[bool],
]


@dataclass(slots=True, frozen=True, kw_only=True)
class ModelCallOutcome:
    message: AssistantMessage
    attempts: int
    retry_started: bool = False
    cancelled: bool = False


class ModelAttemptFailure(Exception):
    """One failed attempt plus its uncommitted partial AssistantMessage."""

    def __init__(self, error: Exception, message: AssistantMessage) -> None:
        self.error = error
        self.message = message
        super().__init__(str(error))


class ModelCallExecutor:
    """Run attempts, deterministic retry waits, Abort, and retry events.

    The executor has no knowledge of Policy, Session, Harness, or tools.
    Callers inject the stream-attempt consumer, which lets AgentLoop and
    governed internal model operations share the same reliability contract.
    """

    def __init__(
        self,
        retry_config: ModelRetryConfig,
        *,
        retry_wait: RetryWait | None = None,
    ) -> None:
        self.retry_config = retry_config
        self._retry_wait = retry_wait or self.wait_for_retry

    async def execute(
        self,
        *,
        model: Model,
        context: AgentContext,
        consume_attempt: ConsumeModelAttempt,
        emit: EventListener | None,
        run_id: str | None,
        turn: int,
        prepare_context: Callable[..., Any] | None,
        signal: AbortSignal | None,
        deadline_event: asyncio.Event | None = None,
    ) -> ModelCallOutcome:
        attempt = 1
        retry_started = False
        while True:
            await notify(
                emit,
                CoreEvent(
                    type="model_start",
                    run_id=run_id,
                    data={
                        "turn": turn,
                        "model": model.name,
                        "attempt": attempt,
                    },
                ),
                signal=signal,
            )
            try:
                message = await consume_attempt(
                    model,
                    context,
                    emit,
                    run_id,
                    attempt=attempt,
                    prepare_context=prepare_context,
                    signal=signal,
                )
            except ModelAttemptFailure as failure:
                error_info = error_info_from_exception(failure.error)
                retry_number = attempt
                delay = self.retry_delay(error_info, retry_number)
                if (
                    delay is None
                    or retry_number > self.retry_config.max_retries
                ):
                    if retry_started:
                        await notify(
                            emit,
                            CoreEvent(
                                type="model_retry_end",
                                run_id=run_id,
                                data={
                                    "success": False,
                                    "cancelled": False,
                                    "attempts": attempt,
                                    "retries": attempt - 1,
                                    "max_retries": (
                                        self.retry_config.max_retries
                                    ),
                                    "error_info": error_info,
                                },
                            ),
                            signal=signal,
                        )
                    raise failure.error from failure

                retry_started = True
                await notify(
                    emit,
                    CoreEvent(
                        type="model_retry_start",
                        run_id=run_id,
                        data={
                            "retry": retry_number,
                            "next_attempt": attempt + 1,
                            "max_retries": self.retry_config.max_retries,
                            "delay": delay,
                            "error_info": error_info,
                        },
                    ),
                    signal=signal,
                )
                if not await self._retry_wait(
                    delay,
                    signal,
                    deadline_event,
                ):
                    aborted = AssistantMessage(
                        content=failure.message.content,
                        stop_reason="aborted",
                        metadata={
                            **failure.message.metadata,
                            "aborted": True,
                            "retry_wait": True,
                        },
                    )
                    await notify(
                        emit,
                        CoreEvent(
                            type="message_start",
                            run_id=run_id,
                            data={
                                "message_id": aborted.id,
                                "role": aborted.role,
                                "attempt": attempt,
                            },
                        ),
                        signal=signal,
                    )
                    return ModelCallOutcome(
                        message=aborted,
                        attempts=attempt,
                        retry_started=True,
                        cancelled=True,
                    )
                attempt += 1
                continue
            return ModelCallOutcome(
                message=message,
                attempts=attempt,
                retry_started=retry_started,
                cancelled=message.stop_reason == "aborted",
            )

    def retry_delay(
        self,
        error_info: ModelErrorInfo | None,
        retry_number: int,
    ) -> float | None:
        config = self.retry_config
        if (
            not config.enabled
            or error_info is None
            or not error_info.retryable
            or retry_number > config.max_retries
        ):
            return None
        if (
            error_info.retry_after is not None
            and error_info.retry_after > config.max_delay
        ):
            return None
        local_delay = min(
            config.base_delay * (2 ** (retry_number - 1)),
            config.max_delay,
        )
        return max(local_delay, error_info.retry_after or 0.0)

    @staticmethod
    async def wait_for_retry(
        delay: float,
        signal: AbortSignal | None,
        deadline_event: asyncio.Event | None = None,
    ) -> bool:
        if signal is None and deadline_event is None:
            await asyncio.sleep(delay)
            return True
        if signal is not None and signal.aborted:
            await signal._wait_until_notified()
            return False
        if deadline_event is not None and deadline_event.is_set():
            return False
        sleep_task = asyncio.create_task(asyncio.sleep(delay))
        race_tasks: set[asyncio.Task[Any]] = {sleep_task}
        abort_task: asyncio.Task[None] | None = None
        deadline_wait: asyncio.Task[Any] | None = None
        if signal is not None:
            abort_task = asyncio.create_task(signal.wait())
            race_tasks.add(abort_task)
        if deadline_event is not None:
            deadline_wait = asyncio.create_task(deadline_event.wait())
            race_tasks.add(deadline_wait)
        done, _ = await asyncio.wait(
            race_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if abort_task is not None and abort_task in done:
            sleep_task.cancel()
            with suppress(asyncio.CancelledError):
                await sleep_task
            await signal._wait_until_notified()  # type: ignore[union-attr]
            return False
        if deadline_wait is not None and deadline_wait in done:
            sleep_task.cancel()
            with suppress(asyncio.CancelledError):
                await sleep_task
            return False
        if abort_task is not None:
            abort_task.cancel()
        if deadline_wait is not None:
            deadline_wait.cancel()
        for task in (abort_task, deadline_wait):
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        await sleep_task
        return True


__all__ = [
    "ConsumeModelAttempt",
    "ModelAttemptFailure",
    "ModelCallExecutor",
    "ModelCallOutcome",
    "RetryWait",
]
