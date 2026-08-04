"""Deliberately broken Confirmation mutant adapters for the kit (HIF-3).

Each mutant breaks exactly one high-risk Confirmation behavior of the
reference adapter.  The validity tests prove that (a) the known-good reference
passes every scenario and (b) every mutant fails only its intended scenario,
so the kit's scenarios are sharp enough to detect the defect they claim to
detect.

The registry maps each mutant name to its factory and the scenario it must
fail.  This mirrors the acceptance matrix in the Task Packet one-to-one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from .conformance import (
    BatchOutcome,
    ConfirmationAdapter,
    ConfirmationRecord,
    ConfirmationResponse,
    ConflictError,
    ExecutedOperation,
    ReopenOutcome,
    RespondOutcome,
    make_response,
)
from .reference import ReferenceConfirmationAdapter


class ReplayOrphanConfirmationMutant(ReferenceConfirmationAdapter):
    """MUTANT: replays (re-executes) requests that should have been orphaned."""

    async def reopen(self, *, runtime_id: str) -> ReopenOutcome:
        orphaned: list[ConfirmationRecord] = []
        for record in list(self._records.values()):
            if record.status == "pending" and record.runtime_id != runtime_id:
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
# Registry (mutant name -> factory, intended failing scenario)
# ---------------------------------------------------------------------------

ConfirmationMutantFactory: TypeAlias = Callable[[], ConfirmationAdapter]

CONFIRMATION_MUTANTS: dict[str, tuple[ConfirmationMutantFactory, str]] = {
    "replay-orphan": (ReplayOrphanConfirmationMutant, "orphan/no replay"),
    "partial-batch": (PartialBatchConfirmationMutant, "atomic batch"),
    "duplicate-accept": (DuplicateAcceptConfirmationMutant, "duplicate response"),
    "timeout-as-abort": (TimeoutAsAbortConfirmationMutant, "timeout/no execution"),
    "expired-accept": (ExpiredResponseAcceptConfirmationMutant, "stale/expired rejection"),
}
