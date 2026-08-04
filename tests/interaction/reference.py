"""Known-good reference Confirmation adapter for the conformance kit (HIF-3).

This adapter implements the observable Confirmation v2 semantics frozen in the
milestone CONTEXT.md section 4 with minimal, self-contained machinery.  It
never imports production modules; Integration binds the approved production
components to the kit Protocols instead.

The kit drives time explicitly (``advance_time``) so every scenario is
deterministic and never depends on real timing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from .conformance import (
    AbortToken,
    BatchOutcome,
    ConflictError,
    ConfirmationRecord,
    ConfirmationRequest,
    ConfirmationResponse,
    ExecutedOperation,
    ReopenOutcome,
    RequestOutcome,
    RespondOutcome,
)

KIT_EPOCH: datetime = datetime(2026, 1, 1, tzinfo=UTC)


class _PendingRecord:
    __slots__ = ("request", "status", "runtime_id", "revision", "response", "deadline", "waiter")

    def __init__(
        self,
        request: ConfirmationRequest,
        runtime_id: str,
        deadline: datetime | None,
    ) -> None:
        self.request = request
        self.status = "pending"  # ConfirmationStatus
        self.runtime_id = runtime_id
        self.revision = 1
        self.response: ConfirmationResponse | None = None
        self.deadline = deadline
        self.waiter = asyncio.Event()


class ReferenceConfirmationAdapter:
    """Known-good Confirmation v2 adapter (deterministic, no real timing)."""

    def __init__(
        self, *, runtime_id: str = "runtime-kit", clock_start: datetime = KIT_EPOCH
    ) -> None:
        self._runtime_id = runtime_id
        self._clock = clock_start
        self._records: dict[str, _PendingRecord] = {}
        self._log: list[ExecutedOperation] = []
        self._closed = False

    # -- kit-driven clock --
    def advance_time(self, seconds: float) -> None:
        self._clock += timedelta(seconds=seconds)
        self._expire_due()

    def _deadline(self, request: ConfirmationRequest) -> datetime | None:
        if request.timeout_seconds is None:
            return None
        return self._clock + timedelta(seconds=request.timeout_seconds)

    def _expire_due(self) -> None:
        for record in list(self._records.values()):
            if (
                record.status == "pending"
                and record.deadline is not None
                and self._clock >= record.deadline
            ):
                self._resolve(record, "expired", self._automatic_deny(record.request.id))

    @staticmethod
    def _automatic_deny(request_id: str) -> ConfirmationResponse:
        return ConfirmationResponse(
            request_id=request_id,
            decision="deny",
            metadata={"automatic": True, "expired": True},
        )

    @staticmethod
    def _automatic_cancel(request_id: str) -> ConfirmationResponse:
        return ConfirmationResponse(
            request_id=request_id,
            decision="cancelled",
            metadata={"automatic": True, "aborted": True},
        )

    def _resolve(
        self, record: _PendingRecord, status: str, response: ConfirmationResponse | None
    ) -> None:
        record.status = status
        record.response = response
        record.revision += 1
        record.waiter.set()

    # -- observable accessors --
    def pending(self) -> tuple[ConfirmationRecord, ...]:
        return tuple(
            ConfirmationRecord(
                request_id=record.request.id,
                status=record.status,
                runtime_id=record.runtime_id,
                revision=record.revision,
                response=record.response,
            )
            for record in self._records.values()
        )

    def execution_log(self) -> tuple[ExecutedOperation, ...]:
        return tuple(self._log)

    # -- request lifecycle --
    async def request(
        self, request: ConfirmationRequest, *, abort: AbortToken | None = None
    ) -> RequestOutcome:
        record = _PendingRecord(request, self._runtime_id, self._deadline(request))
        self._records[request.id] = record
        if abort is None:
            await record.waiter.wait()
        else:
            while not record.waiter.is_set() and not abort.triggered:
                await asyncio.sleep(0)
            if record.status == "pending":
                # the abort signal raced the waiter: resolve as cancelled
                self._resolve(record, "cancelled", self._automatic_cancel(request.id))
        return RequestOutcome(
            request_id=request.id,
            status=record.status,
            response=record.response,
            executed=(record.status == "approved"),
        )

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
        if record.status != "pending":
            return RespondOutcome(
                request_id=response.request_id,
                ok=False,
                error=ConflictError(
                    code=self._resolved_code(record), message=self._resolved_message(record)
                ),
            )
        if response.decision == "approve":
            self._log.append(ExecutedOperation(request_id=record.request.id, decision="approve"))
            self._resolve(record, "approved", response)
        elif response.decision == "deny":
            self._resolve(record, "denied", response)
        else:
            self._resolve(record, "cancelled", response)
        return RespondOutcome(request_id=response.request_id, ok=True, status_after=record.status)

    @staticmethod
    def _resolved_code(record: _PendingRecord) -> str:
        if record.status == "expired":
            return "request_expired"
        if record.status == "orphaned":
            return "request_orphaned"
        return "already_resolved"

    @staticmethod
    def _resolved_message(record: _PendingRecord) -> str:
        return f"request is already {record.status}"

    def _precheck(self, response: ConfirmationResponse) -> ConflictError | None:
        record = self._records.get(response.request_id)
        if record is None:
            return ConflictError(code="unknown_request_id", message="no pending request with that id")
        if record.status == "pending" and record.deadline is not None and self._clock >= record.deadline:
            self._expire_due()
        if record.status != "pending":
            return ConflictError(code=self._resolved_code(record), message=self._resolved_message(record))
        return None

    async def respond_batch(
        self, responses: tuple[ConfirmationResponse, ...]
    ) -> BatchOutcome:
        ids = [response.request_id for response in responses]
        if len(ids) != len(set(ids)):
            return BatchOutcome(
                ok=False, error=ConflictError(code="invalid_batch", message="duplicate request ids in batch")
            )
        # pre-validate every entry before applying anything: all or none
        for response in responses:
            error = self._precheck(response)
            if error is not None:
                return BatchOutcome(ok=False, applied=(), error=error)
        for response in responses:
            record = self._records[response.request_id]
            if response.decision == "approve":
                self._log.append(ExecutedOperation(request_id=response.request_id, decision="approve"))
                self._resolve(record, "approved", response)
            else:
                status = "denied" if response.decision == "deny" else "cancelled"
                self._resolve(record, status, response)
        return BatchOutcome(ok=True, applied=tuple(ids))

    async def abort(self, request_id: str) -> RespondOutcome:
        record = self._records.get(request_id)
        if record is None:
            return RespondOutcome(
                request_id=request_id,
                ok=False,
                error=ConflictError(code="unknown_request_id", message="no pending request with that id"),
            )
        if record.status != "pending":
            return RespondOutcome(
                request_id=request_id,
                ok=False,
                error=ConflictError(
                    code=self._resolved_code(record), message=self._resolved_message(record)
                ),
            )
        self._resolve(record, "cancelled", self._automatic_cancel(request_id))
        return RespondOutcome(request_id=request_id, ok=True, status_after="cancelled")

    # -- process boundary --
    async def close(self) -> None:
        # A runtime process keeps pending records alive while a host reconnects.
        self._closed = True

    async def reopen(self, *, runtime_id: str) -> ReopenOutcome:
        orphaned: list[ConfirmationRecord] = []
        for record in list(self._records.values()):
            if record.status == "pending" and record.runtime_id != runtime_id:
                # no waiter is resumed and no tool is reconstructed
                record.status = "orphaned"
                record.revision += 1
                orphaned.append(
                    ConfirmationRecord(
                        request_id=record.request.id,
                        status="orphaned",
                        runtime_id=record.runtime_id,
                        revision=record.revision,
                        response=None,
                    )
                )
        self._runtime_id = runtime_id
        return ReopenOutcome(orphaned=tuple(orphaned))
