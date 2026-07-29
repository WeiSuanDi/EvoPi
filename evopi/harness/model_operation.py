"""Governed, Session-neutral model operations owned by a Harness."""

from __future__ import annotations

from collections.abc import AsyncIterator
import asyncio
from contextlib import suppress
from uuid import uuid4

from evopi.core.context import AgentContext
from evopi.core.cancellation import AbortController, AbortSignal
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage
from evopi.core.model import Model
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.session.compact import CompactionSettings
from evopi.trace.events import TraceRecord


class GovernedModelOperation:
    """Adapt a one-shot internal model call to the normal Harness reliability path."""

    def __init__(
        self,
        *,
        parent,
        model: Model,
        kind: str,
        signal_controller: AbortController,
    ) -> None:
        self.parent = parent
        self.model = model
        self.kind = kind
        self.operation_id = uuid4().hex
        self.name = model.name
        self.context_window = getattr(model, "context_window", 0)
        self.signal_controller = signal_controller

    async def stream(
        self,
        context: AgentContext,
        *,
        signal: AbortSignal | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        from evopi.harness.base import BaseHarness

        system_prompt = next(
            (message.content for message in context.messages if message.role == "system"),
            "",
        )
        prompt = next(
            (message.content for message in reversed(context.messages) if message.role == "user"),
            "",
        )
        child = BaseHarness(
            model=self.model,
            model_route=self.parent.model_route,
            system_prompt=system_prompt,
            approval_mode="off",
            confirmation_handler=self.parent.confirmation_handler,
            retry_config=self.parent.agent.retry_config,
            compaction_settings=CompactionSettings(enabled=False),
        )
        for policy in self.parent.policies.all():
            child.register_policy(policy, replace=True)

        def observe(event: CoreEvent) -> None:
            if self.parent.trace_writer is None:
                return
            if event.type not in {
                "model_start",
                "model_retry_start",
                "model_retry_end",
                "model_failover_start",
                "model_failover_end",
                "model_circuit_state_changed",
                "model_candidate_skipped",
                "policy_decision",
                "policy_evaluation",
                "confirmation_request",
                "confirmation_response",
                "message_start",
                "message_update",
                "message_end",
                "error",
            }:
                return
            self.parent.trace_writer.write(
                TraceRecord(
                    type=event.type,
                    run_id=event.run_id,
                    data={
                        **event.data,
                        "operation": self.kind,
                        "operation_id": self.operation_id,
                    },
                )
            )

        child.subscribe(observe)
        abort_watcher = asyncio.create_task(
            self._propagate_abort(child, signal)
        )
        try:
            answer: AssistantMessage = await child.prompt(prompt)
        finally:
            abort_watcher.cancel()
            with suppress(asyncio.CancelledError):
                await abort_watcher
            if not child.is_running:
                child.close()
        if answer.stop_reason == "aborted":
            raise RuntimeError("Internal model operation was aborted")
        yield ModelComplete(message=answer)

    async def _propagate_abort(
        self,
        child,
        external_signal: AbortSignal | None,
    ) -> None:
        waits = [
            asyncio.create_task(self.signal_controller.signal.wait())
        ]
        if external_signal is not None:
            waits.append(asyncio.create_task(external_signal.wait()))
        done, pending = await asyncio.wait(
            waits,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            await task
        child.abort()


__all__ = ["GovernedModelOperation"]
