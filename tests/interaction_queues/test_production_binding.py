"""Bind the independent conformance kit to EvoPi's production queue.

The virtual phase driver remains test-only, but every admission, snapshot,
safe-point drain, terminal seal, event, and delivered ``UserMessage`` passes
through :class:`evopi.core.interaction.InteractionQueueController`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, cast

from evopi.core.events import CoreEvent
from evopi.core.interaction import (
    InteractionContentError as CoreContentError,
    InteractionContentTooLargeError as CoreContentTooLargeError,
    InteractionLimits as CoreLimits,
    InteractionModeError as CoreModeError,
    InteractionQueueClosedError as CoreClosedError,
    InteractionQueueController,
    InteractionQueueFullError as CoreFullError,
)
from evopi.core.messages import UserMessage

from .conformance import (
    AdmissionResult,
    CommittedMessage,
    ConformanceFailure,
    EventType,
    INTERACTION_SCENARIOS,
    InteractionError,
    InteractionKind,
    InteractionLimits,
    InteractionMetadata,
    InteractionOrigin,
    InteractionQueueMode,
    InteractionQueueSnapshot,
    InteractionReceipt,
    TerminalReason,
    run_conformance,
)
from .reference import ReferenceInteractionAdapter

_TERMINAL_REASONS = {
    "aborted",
    "deadline_exceeded",
    "error",
    "turn_limit",
    "cancelled",
    "closed",
}


class ProductionInteractionAdapter(ReferenceInteractionAdapter):
    """Drive the production queue through the kit's deterministic phases."""

    def __init__(
        self,
        *,
        steering_mode: InteractionQueueMode = "one-at-a-time",
        follow_up_mode: InteractionQueueMode = "one-at-a-time",
        limits: InteractionLimits | None = None,
    ) -> None:
        super().__init__(
            steering_mode=steering_mode,
            follow_up_mode=follow_up_mode,
            limits=limits,
        )
        effective = self._limits
        self._production = InteractionQueueController(
            steering_mode=steering_mode,
            follow_up_mode=follow_up_mode,
            limits=CoreLimits(
                max_pending_items=effective.max_pending_items,
                max_content_bytes=effective.max_content_bytes,
            ),
        )

    async def steer(
        self,
        content: str,
        *,
        origin: InteractionOrigin = "api",
    ) -> AdmissionResult:
        return await self._admit_production("steer", content, origin)

    async def follow_up(
        self,
        content: str,
        *,
        origin: InteractionOrigin = "api",
    ) -> AdmissionResult:
        return await self._admit_production("follow_up", content, origin)

    async def _admit_production(
        self,
        kind: InteractionKind,
        content: object,
        origin: object,
    ) -> AdmissionResult:
        if self._run_id is None:
            return AdmissionResult(
                error=InteractionError(code="run_not_active", message="no active Run")
            )
        try:
            receipt = await self._production.admit(
                kind,
                cast(Any, origin),
                content,
                emit=self._observe_core_event,
            )
        except CoreContentTooLargeError as exc:
            return AdmissionResult(
                error=InteractionError(
                    code="interaction_content_too_large", message=str(exc)
                )
            )
        except (CoreContentError, CoreModeError) as exc:
            return AdmissionResult(
                error=InteractionError(code="interaction_content_invalid", message=str(exc))
            )
        except CoreFullError as exc:
            return AdmissionResult(
                error=InteractionError(code="interaction_queue_full", message=str(exc))
            )
        except CoreClosedError as exc:
            return AdmissionResult(
                error=InteractionError(code="interaction_closed", message=str(exc))
            )
        return AdmissionResult(
            receipt=InteractionReceipt(
                input_id=receipt.input_id,
                run_id=receipt.run_id,
                kind=receipt.kind,
                origin=receipt.origin,
                created_at=receipt.created_at,
                position=receipt.position,
            )
        )

    def snapshot(self) -> InteractionQueueSnapshot:
        snapshot = self._production.snapshot()
        return InteractionQueueSnapshot(
            steering_mode=snapshot.steering_mode,
            follow_up_mode=snapshot.follow_up_mode,
            pending_steering_count=snapshot.pending_steering_count,
            pending_follow_up_count=snapshot.pending_follow_up_count,
            pending=tuple(
                InteractionReceipt(
                    input_id=item.input_id,
                    run_id=item.run_id,
                    kind=item.kind,
                    origin=item.origin,
                    created_at=item.created_at,
                    position=item.position,
                )
                for item in snapshot.pending
            ),
        )

    async def start_run(self, prompt: str) -> str:
        if self._run_id is not None and not self._ended:
            raise ConformanceFailure("start_run while a Run is still active")
        self._run_index += 1
        self._run_id = f"run-{self._run_index}"
        self._position = 0
        self._turns = 0
        self._attempts = 0
        self._tools_pending = 0
        self._turn_had_tools = False
        self._decision_event = None
        self._confirmation_approved = False
        self._terminate_requested = False
        self._admission_open = True
        self._ended = False
        self._idle_event.clear()
        self._production.open(self._run_id)

        # The yield is the deterministic window used by the initial-admission
        # scenario. The production gate is already open, but Prompt commit and
        # the initial safe point have not happened yet.
        await asyncio.sleep(0)
        self._message_counter += 1
        prompt_id = f"msg-{self._message_counter}"
        self._emit("message_start", {"message_id": prompt_id, "role": "user"})
        self._emit("message_end", {"message_id": prompt_id, "role": "user"})
        self._committed.append(
            CommittedMessage(
                message_id=prompt_id,
                role="user",
                content=prompt,
                interaction=None,
            )
        )
        drained = await self._drain_production("steer")
        if drained == 0:
            self._emit("turn_start", {"turn_number": 1})
        self._turns = 1
        return self._run_id

    async def turn_end(self, *, terminate: bool = False) -> str:
        if self._tools_pending != 0:
            raise ConformanceFailure("turn_end while sibling Tools are still pending")
        if terminate:
            self._terminate_requested = True
        self._emit("turn_end", {"turn_number": self._turns})
        if await self._drain_production("steer"):
            return "continued"
        if self._turn_had_tools:
            self._emit("turn_start", {"turn_number": self._turns + 1})
            self._turns += 1
            return "continued"
        return "stop"

    async def terminal_candidate(self) -> str:
        if self._tools_pending != 0:
            raise ConformanceFailure("terminal_candidate while sibling Tools are pending")
        if self.snapshot().pending_steering_count:
            await self._drain_production("steer")
            return "continued"
        delivered = await self._drain_production("follow_up")
        if delivered:
            return "continued"
        outcome = "terminated" if self._terminate_requested else "completed"
        self._admission_open = False
        self._ended = True
        self._emit("agent_end", {"outcome": outcome})
        self._idle_event.set()
        return outcome

    async def terminate(self, reason: TerminalReason) -> None:
        if reason not in _TERMINAL_REASONS:
            raise ValueError(f"unknown terminal reason {reason!r}")
        await self._production.seal(
            emit=self._observe_core_event,
            run_id=self._run_id,
            reason=reason,
        )
        self._admission_open = False
        self._ended = True
        self._emit("agent_end", {"outcome": reason})
        self._idle_event.set()

    async def _drain_production(self, kind: InteractionKind) -> int:
        if kind == "steer":
            messages = await self._production.drain_steering(
                emit=self._observe_core_event,
                run_id=self._run_id,
                append=self._append_interaction_message,
            )
        else:
            messages = await self._production.drain_follow_up_at_terminal(
                emit=self._observe_core_event,
                run_id=self._run_id,
                reason="terminated" if self._terminate_requested else "completed",
                append=self._append_interaction_message,
            )
        if messages:
            self._emit("turn_start", {"turn_number": self._turns + 1})
            self._turns += 1
        return len(messages)

    async def _observe_core_event(self, event: CoreEvent) -> None:
        data = dict(event.data)
        message = data.pop("message", None)
        if isinstance(message, UserMessage):
            data.update({"message_id": message.id, "role": message.role})
        self._emit(cast(EventType, event.type), data)

    def _append_interaction_message(self, message: UserMessage) -> None:
        raw = message.metadata.get("interaction")
        if not isinstance(raw, dict):
            raise ConformanceFailure("delivered UserMessage lacks interaction metadata")
        created_at = raw.get("created_at")
        if not isinstance(created_at, str):
            raise ConformanceFailure("interaction metadata lacks created_at")
        metadata = InteractionMetadata(
            schema_version=cast(int, raw["schema_version"]),
            input_id=cast(str, raw["input_id"]),
            kind=cast(InteractionKind, raw["kind"]),
            origin=cast(InteractionOrigin, raw["origin"]),
            created_at=datetime.fromisoformat(created_at),
        )
        self._committed.append(
            CommittedMessage(
                message_id=message.id,
                role=message.role,
                content=message.content,
                interaction=metadata,
            )
        )


def test_production_queue_passes_the_full_independent_conformance_suite() -> None:
    results = run_conformance(ProductionInteractionAdapter)
    failures = {name: status for name, status in results.items() if status != "ok"}
    assert not failures, failures
    assert len(results) == len(INTERACTION_SCENARIOS)
