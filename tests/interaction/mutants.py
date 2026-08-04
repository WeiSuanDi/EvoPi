"""Deliberately broken mutant adapters for the conformance kit (HIF-3).

Each mutant breaks exactly one high-risk behavior of the reference adapter.
The validity tests prove that (a) the known-good reference passes every
scenario and (b) every mutant fails only its intended scenario, so the kit's
scenarios are sharp enough to detect the defect they claim to detect.

The registries map each mutant name to its factory and the scenario it must
fail.  This mirrors the acceptance matrix in the Task Packet one-to-one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeAlias

from .conformance import (
    BatchOutcome,
    ConfirmationAdapter,
    ConfirmationRecord,
    ConfirmationResponse,
    ConflictError,
    ExecutedOperation,
    ReopenOutcome,
    ReplayResult,
    RespondOutcome,
    RpcAdapter,
    RpcErrorInfo,
    RpcEvent,
    RpcResponse,
    make_response,
)
from .reference import ReferenceConfirmationAdapter, ReferenceRpcAdapter

# ---------------------------------------------------------------------------
# Confirmation mutants (TASK.md Task 2)
# ---------------------------------------------------------------------------


class ReplayOrphanConfirmationMutant(ReferenceConfirmationAdapter):
    """MUTANT: replays (re-executes) requests that should have been orphaned."""

    async def recover(self, *, runtime_id: str) -> ReopenOutcome:
        orphaned: list[ConfirmationRecord] = []
        for record in list(self._records.values()):
            if record.status == "pending":
                # broken: the pending operation is resumed and executed
                self._log.append(ExecutedOperation(request_id=record.request.id, decision="approve"))
                self._resolve(
                    record, "approved", make_response(request_id=record.request.id, decision="approve")
                )
                orphaned.append(
                    ConfirmationRecord(
                        request_id=record.request.id,
                        status="approved",
                        runtime_id=record.runtime_id,
                        revision=record.revision,
                        response=record.response,
                    )
                )
        self._runtime_id = runtime_id
        return ReopenOutcome(orphaned=tuple(orphaned))


class PartialBatchConfirmationMutant(ReferenceConfirmationAdapter):
    """MUTANT: applies a batch partially instead of atomically."""

    async def respond_batch(
        self, responses: tuple[ConfirmationResponse, ...]
    ) -> BatchOutcome:
        applied: list[str] = []
        for response in responses:  # broken: applies entry by entry, stopping at errors
            result = await self.respond(response)
            if not result.ok:
                return BatchOutcome(ok=False, applied=tuple(applied), error=result.error)
            applied.append(response.request_id)
        return BatchOutcome(ok=True, applied=tuple(applied))


class DuplicateAcceptConfirmationMutant(ReferenceConfirmationAdapter):
    """MUTANT: accepts a duplicate response and applies it a second time."""

    async def respond(self, response: ConfirmationResponse) -> RespondOutcome:
        record = self._records.get(response.request_id)
        if record is None:
            return RespondOutcome(
                request_id=response.request_id,
                ok=False,
                error=ConflictError(code="unknown_request_id", message="no pending request with that id"),
            )
        if record.status == "pending" and record.deadline is not None and self._clock >= record.deadline:
            self._expire_due()
        if record.status == "pending":
            return await super().respond(response)
        if record.status == "approved":
            # broken: a second approval for the same request is accepted and
            # re-applied (a second effect) instead of rejected
            self._log.append(ExecutedOperation(request_id=record.request.id, decision="approve"))
            record.response = response
            record.revision += 1
            return RespondOutcome(request_id=response.request_id, ok=True, status_after=record.status)
        return RespondOutcome(
            request_id=response.request_id,
            ok=False,
            error=ConflictError(
                code=self._resolved_code(record), message=self._resolved_message(record)
            ),
        )


class TimeoutAsAbortConfirmationMutant(ReferenceConfirmationAdapter):
    """MUTANT: treats timeout as an abort instead of an expiry denial."""

    def _expire_due(self) -> None:
        for record in list(self._records.values()):
            if (
                record.status == "pending"
                and record.deadline is not None
                and self._clock >= record.deadline
            ):
                # broken: timeout is resolved as cancelled with aborted metadata
                self._resolve(record, "cancelled", self._automatic_cancel(record.request.id))


class ExpiredResponseAcceptConfirmationMutant(ReferenceConfirmationAdapter):
    """MUTANT: accepts a response for a request that already expired."""

    async def respond(self, response: ConfirmationResponse) -> RespondOutcome:
        record = self._records.get(response.request_id)
        if record is None:
            return RespondOutcome(
                request_id=response.request_id,
                ok=False,
                error=ConflictError(code="unknown_request_id", message="no pending request with that id"),
            )
        if record.status == "pending" and record.deadline is not None and self._clock >= record.deadline:
            self._expire_due()
        if record.status == "pending":
            return await super().respond(response)
        if record.status == "expired":
            # broken: the expired request still accepts the response and executes
            if response.decision == "approve":
                self._log.append(ExecutedOperation(request_id=record.request.id, decision="approve"))
            status = (
                "approved"
                if response.decision == "approve"
                else "denied" if response.decision == "deny" else "cancelled"
            )
            self._resolve(record, status, response)
            return RespondOutcome(request_id=response.request_id, ok=True, status_after=record.status)
        return RespondOutcome(
            request_id=response.request_id,
            ok=False,
            error=ConflictError(
                code=self._resolved_code(record), message=self._resolved_message(record)
            ),
        )


# ---------------------------------------------------------------------------
# RPC / Event Stream mutants (TASK.md Task 3)
# ---------------------------------------------------------------------------


class SkippedEventRpcMutant(ReferenceRpcAdapter):
    """MUTANT: silently drops the retained/live boundary event for subscribers."""

    def _retained_for_subscriber(self, after_sequence: int) -> list[RpcEvent]:
        # broken: the event at the retained/live boundary is never delivered
        return [
            event
            for event in super()._retained_for_subscriber(after_sequence)
            if event.sequence != self.retained_capacity
        ]


class DuplicateReplayRpcMutant(ReferenceRpcAdapter):
    """MUTANT: delivers every retained event twice via direct replay."""

    def replay(self, *, after_sequence: int) -> ReplayResult:
        result = super().replay(after_sequence=after_sequence)
        if not result.ok:
            return result
        doubled: list[RpcEvent] = []
        for event in result.events:
            doubled.extend([event, event])
        return ReplayResult(ok=True, events=tuple(doubled))


class StaleCursorSilentSkipRpcMutant(ReferenceRpcAdapter):
    """MUTANT: silently returns an empty result for an expired cursor."""

    def replay(self, *, after_sequence: int) -> ReplayResult:
        result = super().replay(after_sequence=after_sequence)
        if not result.ok:
            # broken: the expired cursor yields ok with no events instead of an
            # explicit cursor-expired error
            return ReplayResult(ok=True, events=(), error=None)
        return result


class BlockedReaderRpcMutant(ReferenceRpcAdapter):
    """MUTANT: the publisher awaits slow subscribers, blocking event production."""

    async def publish(self, type_: str, data: dict[str, Any]) -> RpcEvent:
        # broken: waits for subscriber capacity instead of failing the subscriber
        for subscriber in self._subscribers:
            while (
                not subscriber._closed
                and subscriber._failure is None
                and len(subscriber._queue) >= subscriber._max_queue
            ):
                await asyncio.sleep(0)
        return await super().publish(type_, data)


class DuplicateDispatchRpcMutant(ReferenceRpcAdapter):
    """MUTANT: dispatches every request to its handler twice."""

    async def _dispatch(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> RpcResponse:
        # broken: the handler runs twice per request id; the first response is
        # returned so only the double effect (and double dispatch log) shows
        first = await super()._dispatch(request_id, method, params)
        await super()._dispatch(request_id, method, params)
        return first


class ExceptionLeakRpcMutant(ReferenceRpcAdapter):
    """MUTANT: leaks raw exception text into RPC error responses."""

    @staticmethod
    def _format_unexpected(exc: Exception) -> RpcErrorInfo:
        # broken: the exception message (and its secrets) reaches the client
        return RpcErrorInfo(code="internal_error", message=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Registries (mutant name -> factory, intended failing scenario)
# ---------------------------------------------------------------------------

ConfirmationMutantFactory: TypeAlias = Callable[[], ConfirmationAdapter]
RpcMutantFactory: TypeAlias = Callable[[], RpcAdapter]

CONFIRMATION_MUTANTS: dict[str, tuple[ConfirmationMutantFactory, str]] = {
    "replay-orphan": (ReplayOrphanConfirmationMutant, "orphan/no replay"),
    "partial-batch": (PartialBatchConfirmationMutant, "atomic batch"),
    "duplicate-accept": (DuplicateAcceptConfirmationMutant, "duplicate response"),
    "timeout-as-abort": (TimeoutAsAbortConfirmationMutant, "timeout/no execution"),
    "expired-accept": (ExpiredResponseAcceptConfirmationMutant, "stale/expired rejection"),
}

RPC_MUTANTS: dict[str, tuple[RpcMutantFactory, str]] = {
    "skipped-event": (SkippedEventRpcMutant, "replay/live handoff"),
    "duplicate-replay": (DuplicateReplayRpcMutant, "event uniqueness"),
    "stale-cursor-silent-skip": (StaleCursorSilentSkipRpcMutant, "cursor expiration"),
    "blocked-reader": (BlockedReaderRpcMutant, "slow subscriber failure"),
    "duplicate-dispatch": (DuplicateDispatchRpcMutant, "duplicate request ID"),
    "exception-leak": (ExceptionLeakRpcMutant, "RPC redaction"),
}
