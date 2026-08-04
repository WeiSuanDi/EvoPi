"""Coordination of pending confirmation requests against a ConfirmationStore.

The broker keeps one pending waiter per request id and races Abort, an
already-committed response, and timeout with priority
``Abort > committed response > timeout``. Every outcome persists one terminal
transition; nothing ever executes a tool or replays a coroutine here.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from evopi.core.cancellation import AbortSignal
from evopi.harness.confirmation import (
    ConfirmationBatchResponse,
    ConfirmationBrokerClosedError,
    ConfirmationDecision,
    ConfirmationDuplicateResponseError,
    ConfirmationRecord,
    ConfirmationRequest,
    ConfirmationResponse,
    ConfirmationSettings,
    ConfirmationStatus,
    ConfirmationStore,
    ConfirmationTransition,
    ConfirmationUnknownRequestError,
)
from evopi.harness.confirmation_store import already_resolved_error


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _decision_status(decision: ConfirmationDecision) -> ConfirmationStatus:
    if decision == "approve":
        return "approved"
    if decision == "deny":
        return "denied"
    return "cancelled"


class ConfirmationBroker:
    """One pending waiter per request id; fail-closed on close.

    ``request()`` creates the durable record before waiting, so a response,
    timeout, or abort always has a persisted terminal transition to reconcile
    with.
    """

    def __init__(
        self,
        store: ConfirmationStore,
        *,
        runtime_id: str | None = None,
        settings: ConfirmationSettings | None = None,
    ) -> None:
        self._store = store
        self._runtime_id = runtime_id or uuid4().hex
        self._settings = settings or ConfirmationSettings()
        self._waiters: dict[str, asyncio.Future[ConfirmationResponse]] = {}
        self._closed = False

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    def list_pending(self) -> tuple[ConfirmationRecord, ...]:
        self._check_open()
        return self._store.list_pending()

    async def request(
        self,
        request: ConfirmationRequest,
        *,
        signal: AbortSignal | None = None,
    ) -> ConfirmationResponse:
        self._check_open()
        record = ConfirmationRecord(
            request=request,
            status="pending",
            runtime_id=self._runtime_id,
            revision=1,
            updated_at=_utc_now(),
        )
        self._store.create(record)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ConfirmationResponse] = loop.create_future()
        self._waiters[request.id] = future
        try:
            return await self._race(record, future, signal=signal)
        except asyncio.CancelledError:
            # External cancellation: fail closed with one cancelled
            # transition, then re-raise (Finding D, rev 2).
            response = ConfirmationResponse(
                request_id=request.id,
                decision="cancelled",
                reason="Confirmation wait cancelled",
                metadata={"automatic": True, "cancelled": True},
            )
            self._persist_terminal(record, "cancelled", response)
            raise
        finally:
            self._waiters.pop(request.id, None)

    async def submit(self, response: ConfirmationResponse) -> ConfirmationRecord:
        """Commit one response, waking its waiter only after persistence."""
        self._check_open()
        current = self._store.get(response.request_id)
        if current is None:
            raise ConfirmationUnknownRequestError(
                f"no confirmation request {response.request_id!r}",
                details={"request_id": response.request_id},
            )
        if current.status != "pending":
            raise already_resolved_error(current)
        status = _decision_status(response.decision)
        record = self._store.transition(
            response.request_id,
            expected_revision=current.revision,
            status=status,
            response=response,
        )
        future = self._waiters.get(response.request_id)
        if future is not None and not future.done():
            future.set_result(response)
        return record

    async def submit_batch(
        self, batch: ConfirmationBatchResponse
    ) -> tuple[ConfirmationRecord, ...]:
        """Commit an atomic batch; no transition happens on any failure."""
        self._check_open()
        responses = batch.responses
        seen: set[str] = set()
        transitions: list[ConfirmationTransition] = []
        for response in responses:
            if response.request_id in seen:
                raise ConfirmationDuplicateResponseError(
                    f"duplicate response for request {response.request_id!r} in batch",
                    details={"request_id": response.request_id},
                )
            seen.add(response.request_id)
            current = self._store.get(response.request_id)
            if current is None:
                raise ConfirmationUnknownRequestError(
                    f"no confirmation request {response.request_id!r}",
                    details={"request_id": response.request_id},
                )
            if current.status != "pending":
                raise already_resolved_error(current)
            transitions.append(
                ConfirmationTransition(
                    request_id=response.request_id,
                    expected_revision=current.revision,
                    status=_decision_status(response.decision),
                    response=response,
                )
            )
        if not transitions:
            return ()
        records = self._store.transition_batch(tuple(transitions))
        for response in responses:
            future = self._waiters.get(response.request_id)
            if future is not None and not future.done():
                future.set_result(response)
        return records

    def close(self) -> None:
        """Graceful close: persist `cancelled`, wake callers, close once.

        Every still-pending live waiter is atomically transitioned to
        ``cancelled`` (with a correlated automatic response carrying
        ``closed=true``) BEFORE any caller is woken, so graceful close is
        durable and never degrades to a crash-orphan. A response already
        committed before close remains authoritative. The method stays
        synchronous, idempotent, and exactly-once (Finding H, rev 3).
        """
        if self._closed:
            return
        self._closed = True
        waiters = dict(self._waiters)
        self._waiters.clear()
        failure: Exception | None = None
        try:
            transitions: list[ConfirmationTransition] = []
            for request_id, future in waiters.items():
                current = self._store.get(request_id)
                if current is None or current.status != "pending":
                    continue  # already resolved: authoritative, never overwritten
                transitions.append(
                    ConfirmationTransition(
                        request_id=request_id,
                        expected_revision=current.revision,
                        status="cancelled",
                        response=ConfirmationResponse(
                            request_id=request_id,
                            decision="cancelled",
                            reason="Confirmation broker closed",
                            metadata={"automatic": True, "closed": True},
                        ),
                    )
                )
            if transitions:
                # Persist first: one atomic batch transition per graceful close.
                self._store.transition_batch(tuple(transitions))
        except Exception as exc:
            # The persistence failure remains visible to the owner, but it may
            # never strand a live Run. A durable pending fact can only become
            # orphaned on recovery; no waiter or Tool is reconstructed.
            failure = exc
        finally:
            for future in waiters.values():
                if not future.done():
                    future.cancel()
            try:
                self._store.close()
            except Exception as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    def _check_open(self) -> None:
        if self._closed:
            raise ConfirmationBrokerClosedError("confirmation broker is closed")

    async def _race(
        self,
        record: ConfirmationRecord,
        future: asyncio.Future[ConfirmationResponse],
        *,
        signal: AbortSignal | None,
    ) -> ConfirmationResponse:
        tasks: set[asyncio.Future[Any]] = {future}
        abort_task: asyncio.Task[None] | None = None
        timeout_task: asyncio.Task[None] | None = None

        if signal is not None:
            abort_task = asyncio.create_task(signal.wait())
            tasks.add(abort_task)
        delay = self._timeout_delay(record.request)
        if delay is not None:
            timeout_task = asyncio.create_task(asyncio.sleep(delay))
            tasks.add(timeout_task)

        try:
            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # External cancellation of the waiting task: cancel and retrieve
            # every race Task/Future so nothing stays unresolved.
            await self._cancel_all_tasks(tasks)
            raise

        # Priority: Abort > already-committed response > timeout.
        if abort_task is not None and abort_task in done:
            await self._cancel_losers({abort_task}, tasks)
            future.cancel()
            return self._abort_outcome(record)
        if future in done:
            await self._cancel_losers({future}, tasks)
            if future.cancelled():
                raise ConfirmationBrokerClosedError("confirmation broker is closed")
            return future.result()
        if timeout_task is not None and timeout_task in done:
            await self._cancel_losers({timeout_task}, tasks)
            future.cancel()
            return self._timeout_outcome(record)
        raise AssertionError("unreachable race state")  # pragma: no cover

    async def _cancel_all_tasks(
        self, tasks: set[asyncio.Future[Any]]
    ) -> None:
        for task in tasks:
            if isinstance(task, asyncio.Task):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            elif not task.done():
                task.cancel()

    async def _cancel_losers(
        self,
        winners: set[asyncio.Future[Any]],
        tasks: set[asyncio.Future[Any]],
    ) -> None:
        for task in tasks - winners:
            if isinstance(task, asyncio.Task):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def _timeout_delay(self, request: ConfirmationRequest) -> float | None:
        deadline: datetime | None = None
        if request.expires_at is not None:
            deadline = request.expires_at
        elif self._settings.timeout_seconds is not None:
            deadline = _utc_now() + timedelta(seconds=self._settings.timeout_seconds)
        if deadline is None:
            return None
        return max(0.0, (deadline - _utc_now()).total_seconds())

    def _abort_outcome(self, record: ConfirmationRecord) -> ConfirmationResponse:
        response = ConfirmationResponse(
            request_id=record.request.id,
            decision="cancelled",
            reason="Confirmation aborted",
            metadata={"automatic": True, "aborted": True},
        )
        self._persist_terminal(record, "cancelled", response)
        return response

    def _timeout_outcome(self, record: ConfirmationRecord) -> ConfirmationResponse:
        response = ConfirmationResponse(
            request_id=record.request.id,
            decision="deny",
            reason="Confirmation request timed out",
            metadata={"automatic": True, "expired": True},
        )
        self._persist_terminal(record, "expired", response)
        return response

    def _persist_terminal(
        self,
        record: ConfirmationRecord,
        status: ConfirmationStatus,
        response: ConfirmationResponse,
    ) -> None:
        """Persist one terminal transition; already-resolved records win."""
        current = self._store.get(record.request.id)
        if current is None or current.status != "pending":
            return
        self._store.transition(
            record.request.id,
            expected_revision=current.revision,
            status=status,
            response=response,
        )


__all__ = ["ConfirmationBroker"]
