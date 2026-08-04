"""Failing-first deterministic tests for ConfirmationBroker races."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from evopi.core.cancellation import AbortController
from evopi.harness.confirmation import (
    ConfirmationBatchResponse,
    ConfirmationBrokerClosedError,
    ConfirmationDuplicateResponseError,
    ConfirmationExpiredError,
    ConfirmationRecord,
    ConfirmationRequest,
    ConfirmationResponse,
    ConfirmationSettings,
    ConfirmationStoreClosedError,
    ConfirmationTransition,
    ConfirmationUnknownRequestError,
)
from evopi.harness.confirmation_broker import ConfirmationBroker
from evopi.harness.confirmation_store import (
    ConfirmationFileStore,
    InMemoryConfirmationStore,
)


def _request(request_id: str) -> ConfirmationRequest:
    return ConfirmationRequest(
        hook="before_tool_call", reason="requires approval", id=request_id
    )


def _expiring_request(request_id: str) -> ConfirmationRequest:
    # A past deadline fires the timeout on the first loop pass, keeping the
    # race deterministic without wall-clock sleeps (Finding F: timeout_seconds
    # must be strictly positive, so settings cannot express an instant fire).
    return ConfirmationRequest(
        hook="before_tool_call",
        reason="requires approval",
        id=request_id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )


class _RecordingStore(InMemoryConfirmationStore):
    """In-memory store that captures atomic batches for close-boundary tests."""

    def __init__(self) -> None:
        super().__init__()
        self.transitions: list[tuple[ConfirmationTransition, ...]] = []

    def transition_batch(
        self,
        transitions: tuple[ConfirmationTransition, ...],
    ) -> tuple[ConfirmationRecord, ...]:
        self.transitions.append(tuple(transitions))
        return super().transition_batch(transitions)


def _response(request_id: str, decision: str = "approve") -> ConfirmationResponse:
    return ConfirmationResponse(
        request_id=request_id,
        decision=decision,  # type: ignore[arg-type]
        reason="ok",
        metadata={"source": "test"},
    )


def test_request_creates_pending_record_before_waiting() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)

        pending = broker.list_pending()
        assert [r.request.id for r in pending] == ["req-1"]
        assert pending[0].status == "pending"
        assert pending[0].revision == 1
        assert pending[0].runtime_id == broker.runtime_id

        record = await broker.submit(_response("req-1"))
        result = await task

        assert result.decision == "approve"
        assert record.status == "approved"
        assert record.revision == 2
        assert broker.list_pending() == ()

    asyncio.run(scenario())


def test_signal_without_abort_waits_for_submit() -> None:
    async def scenario() -> None:
        controller = AbortController(loop=asyncio.get_running_loop())
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        task = asyncio.create_task(
            broker.request(_request("req-1"), signal=controller.signal)
        )
        await asyncio.sleep(0)
        assert broker.list_pending()  # still waiting, not aborted

        result = await asyncio.gather(broker.submit(_response("req-1")), task)

        assert result[1].decision == "approve"

    asyncio.run(scenario())


def test_timeout_persists_expired_and_returns_automatic_deny() -> None:
    async def scenario() -> None:
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        result = await broker.request(_expiring_request("req-1"))

        assert result.decision == "deny"
        assert result.metadata == {"automatic": True, "expired": True}
        assert "aborted" not in result.metadata
        record = store.get("req-1")
        assert record is not None
        assert record.status == "expired"
        assert record.response is not None
        assert record.response.metadata["expired"] is True

    asyncio.run(scenario())


def test_timeout_is_not_an_abort() -> None:
    async def scenario() -> None:
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        result = await broker.request(_expiring_request("req-1"))
        record = store.get("req-1")
        assert result.decision == "deny"
        assert record is not None and record.status == "expired"
        assert record.response is not None and "aborted" not in record.response.metadata

    asyncio.run(scenario())


def test_committed_response_beats_timeout_with_one_transition() -> None:
    async def scenario() -> None:
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(broker.request(_expiring_request("req-1")))
        await asyncio.sleep(0)  # record created, race (with timeout) awaiting

        # The commit completes before the race round, so the response wins.
        await broker.submit(_response("req-1"))
        result = await task

        assert result.decision == "approve"
        record = store.get("req-1")
        assert record is not None
        assert record.status == "approved"
        assert record.revision == 2  # exactly one terminal transition

    asyncio.run(scenario())


def test_abort_beats_timeout() -> None:
    async def scenario() -> None:
        controller = AbortController(loop=asyncio.get_running_loop())
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(
            broker.request(_expiring_request("req-1"), signal=controller.signal)
        )
        await asyncio.sleep(0)
        controller.abort()

        result = await task

        assert result.decision == "cancelled"
        assert result.metadata == {"automatic": True, "aborted": True}
        record = store.get("req-1")
        assert record is not None
        assert record.status == "cancelled"
        assert record.response is not None
        assert record.response.metadata["aborted"] is True

    asyncio.run(scenario())


def test_abort_without_timeout() -> None:
    async def scenario() -> None:
        controller = AbortController(loop=asyncio.get_running_loop())
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(
            broker.request(_request("req-1"), signal=controller.signal)
        )
        await asyncio.sleep(0)
        controller.abort()

        result = await task

        assert result.decision == "cancelled"
        assert result.metadata["aborted"] is True
        record = store.get("req-1")
        assert record is not None and record.status == "cancelled"

    asyncio.run(scenario())


def test_abort_beats_already_committed_response() -> None:
    async def scenario() -> None:
        controller = AbortController(loop=asyncio.get_running_loop())
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(
            broker.request(_request("req-1"), signal=controller.signal)
        )
        await asyncio.sleep(0)  # record created, race awaiting

        # Abort latches and its wait task completes before the response commits,
        # so the race round observes both done and Abort wins.
        controller.abort()
        await asyncio.sleep(0)
        await broker.submit(_response("req-1"))
        result = await task

        # Abort > already-committed response: the caller fails closed.
        assert result.decision == "cancelled"
        assert result.metadata["aborted"] is True
        # The store keeps the first terminal transition only.
        record = store.get("req-1")
        assert record is not None
        assert record.status == "approved"
        assert record.revision == 2

    asyncio.run(scenario())


def test_duplicate_response_rejected_without_second_effect() -> None:
    async def scenario() -> None:
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)

        await broker.submit(_response("req-1"))
        with pytest.raises(ConfirmationDuplicateResponseError):
            await broker.submit(_response("req-1"))

        result = await task
        assert result.decision == "approve"
        record = store.get("req-1")
        assert record is not None and record.revision == 2

    asyncio.run(scenario())


def test_unknown_request_rejected() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        with pytest.raises(ConfirmationUnknownRequestError):
            await broker.submit(_response("req-missing"))

    asyncio.run(scenario())


def test_submit_after_timeout_raises_expired() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        result = await broker.request(_expiring_request("req-1"))
        assert result.decision == "deny"

        with pytest.raises(ConfirmationExpiredError):
            await broker.submit(_response("req-1"))

    asyncio.run(scenario())


def test_batch_success_wakes_all_waiters_atomically() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        tasks = [
            asyncio.create_task(broker.request(_request("req-a"))),
            asyncio.create_task(broker.request(_request("req-b"))),
        ]
        await asyncio.sleep(0)

        records = await broker.submit_batch(
            ConfirmationBatchResponse(
                responses=(
                    _response("req-a"),
                    _response("req-b", decision="deny"),
                )
            )
        )
        results = await asyncio.gather(*tasks)

        assert [r.decision for r in results] == ["approve", "deny"]
        assert [r.status for r in records] == ["approved", "denied"]

    asyncio.run(scenario())


def test_batch_invalid_item_transitions_nothing() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        tasks = [
            asyncio.create_task(broker.request(_request("req-a"))),
            asyncio.create_task(broker.request(_request("req-b"))),
        ]
        await asyncio.sleep(0)

        with pytest.raises(ConfirmationUnknownRequestError):
            await broker.submit_batch(
                ConfirmationBatchResponse(
                    responses=(
                        _response("req-a"),
                        _response("req-missing"),
                    )
                )
            )

        # No transition happened and no waiter was woken.
        assert [r.status for r in broker.list_pending()] == ["pending", "pending"]
        broker.close()
        with pytest.raises(ConfirmationBrokerClosedError):
            await tasks[0]

    asyncio.run(scenario())


def test_batch_duplicate_ids_rejected_before_any_transition() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        task = asyncio.create_task(broker.request(_request("req-a")))
        await asyncio.sleep(0)

        with pytest.raises(ConfirmationDuplicateResponseError):
            await broker.submit_batch(
                ConfirmationBatchResponse(
                    responses=(_response("req-a"), _response("req-a"))
                )
            )

        assert [r.status for r in broker.list_pending()] == ["pending"]
        broker.close()
        with pytest.raises(ConfirmationBrokerClosedError):
            await task

    asyncio.run(scenario())


def test_closed_broker_rejects_requests_and_submits() -> None:
    async def scenario() -> None:
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        broker.close()

        with pytest.raises(ConfirmationBrokerClosedError):
            await broker.request(_request("req-1"))
        with pytest.raises(ConfirmationBrokerClosedError):
            await broker.submit(_response("req-1"))
        with pytest.raises(ConfirmationBrokerClosedError):
            await broker.submit_batch(
                ConfirmationBatchResponse(responses=(_response("req-1"),))
            )
        with pytest.raises(ConfirmationBrokerClosedError):
            broker.list_pending()
        # Nothing was created, and the closed store rejects reads too.
        with pytest.raises(ConfirmationStoreClosedError):
            store.get("req-1")

    asyncio.run(scenario())


def test_close_cancels_pending_waits_fail_closed() -> None:
    async def scenario() -> None:
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)
        assert broker.list_pending()

        broker.close()
        broker.close()  # idempotent: the store closes exactly once

        with pytest.raises(ConfirmationBrokerClosedError):
            await task

    asyncio.run(scenario())


def test_default_settings_waits_forever_until_close() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert broker.list_pending()  # no timeout fires

        broker.close()
        with pytest.raises(ConfirmationBrokerClosedError):
            await task

    asyncio.run(scenario())


def test_no_unresolved_tasks_after_timeout() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        result = await broker.request(_expiring_request("req-1"))
        assert result.decision == "deny"

        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if not t.done() and t is not current]
        assert pending == []

    asyncio.run(scenario())


def test_settings_timeout_must_be_finite_and_positive() -> None:
    for bad in (0, -1, 0.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="timeout_seconds"):
            ConfirmationSettings(timeout_seconds=bad)  # type: ignore[arg-type]
    assert ConfirmationSettings(timeout_seconds=0.001).timeout_seconds == 0.001
    assert ConfirmationSettings().timeout_seconds is None


def test_external_cancellation_persists_cancelled_and_cleans_up() -> None:
    async def scenario() -> None:
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)

        task.cancel()
        try:
            await task
            raise AssertionError("expected CancelledError")
        except asyncio.CancelledError:
            pass

        record = store.get("req-1")
        assert record is not None
        assert record.status == "cancelled"
        assert record.response is not None
        assert record.response.decision == "cancelled"
        assert record.response.metadata == {"automatic": True, "cancelled": True}

        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if not t.done() and t is not current]
        assert pending == []

    asyncio.run(scenario())


def test_external_cancellation_with_timeout_cleans_race_tasks() -> None:
    async def scenario() -> None:
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(broker.request(_expiring_request("req-1")))
        await asyncio.sleep(0)  # race created with a timeout task

        task.cancel()
        try:
            await task
            raise AssertionError("expected CancelledError")
        except asyncio.CancelledError:
            pass

        record = store.get("req-1")
        assert record is not None
        assert record.status == "cancelled"
        assert record.response is not None
        assert record.response.metadata == {"automatic": True, "cancelled": True}

        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if not t.done() and t is not current]
        assert pending == []

    asyncio.run(scenario())


def test_cancelled_wait_rejects_late_response() -> None:
    async def scenario() -> None:
        store = InMemoryConfirmationStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)

        task.cancel()
        await asyncio.sleep(0)  # cancellation handler persists `cancelled`
        with pytest.raises(ConfirmationDuplicateResponseError):
            await broker.submit(_response("req-1"))

        record = store.get("req-1")
        assert record is not None
        assert record.status == "cancelled"
        assert record.revision == 2  # exactly one terminal transition

    asyncio.run(scenario())


def test_close_atomically_persists_cancelled_for_all_pending_waiters() -> None:
    """Finding H (rev 3): graceful close persists one atomic cancelled batch."""
    async def scenario() -> None:
        store = _RecordingStore()
        broker = ConfirmationBroker(store)
        tasks = [
            asyncio.create_task(broker.request(_request("req-a"))),
            asyncio.create_task(broker.request(_request("req-b"))),
        ]
        await asyncio.sleep(0)

        broker.close()

        for task in tasks:
            with pytest.raises(ConfirmationBrokerClosedError):
                await task

        # One atomic batch, persisted before the callers were woken.
        assert len(store.transitions) == 1
        batch = store.transitions[0]
        assert [t.request_id for t in batch] == ["req-a", "req-b"]
        for transition in batch:
            assert transition.status == "cancelled"
            assert transition.response is not None
            assert transition.response.decision == "cancelled"
            assert transition.response.metadata == {"automatic": True, "closed": True}

    asyncio.run(scenario())


def test_close_preserves_committed_response() -> None:
    """Finding H (rev 3): a committed response is never overwritten by close."""
    async def scenario() -> None:
        store = _RecordingStore()
        broker = ConfirmationBroker(store)
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)

        await broker.submit(_response("req-1"))
        broker.close()

        result = await task
        assert result.decision == "approve"
        assert store.transitions == []  # no close transition overwrote it

    asyncio.run(scenario())


def test_file_store_graceful_close_reopens_as_cancelled(tmp_path) -> None:
    """Finding H (rev 3): GRACEFUL_CLOSE_STATUS must be cancelled, not orphaned."""
    async def scenario() -> None:
        root = tmp_path / "store"
        store = ConfirmationFileStore(root, runtime_id="broker-1")
        broker = ConfirmationBroker(store, runtime_id="broker-1")
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)

        broker.close()
        with pytest.raises(ConfirmationBrokerClosedError):
            await task

        for runtime_id in ("broker-1", "broker-2"):
            reopened = ConfirmationFileStore(root, runtime_id=runtime_id)
            record = reopened.get("req-1")
            assert record is not None
            assert record.status == "cancelled"
            assert record.response is not None
            assert record.response.decision == "cancelled"
            assert record.response.metadata == {"automatic": True, "closed": True}
            assert reopened.list_pending() == ()
            reopened.close()

    asyncio.run(scenario())


def test_file_store_graceful_close_preserves_committed_response(tmp_path) -> None:
    """Finding H (rev 3): committed approvals survive close and reopen."""
    async def scenario() -> None:
        root = tmp_path / "store"
        store = ConfirmationFileStore(root, runtime_id="broker-1")
        broker = ConfirmationBroker(store, runtime_id="broker-1")
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)

        await broker.submit(_response("req-1"))
        broker.close()
        result = await task
        assert result.decision == "approve"

        reopened = ConfirmationFileStore(root, runtime_id="broker-1")
        record = reopened.get("req-1")
        assert record is not None
        assert record.status == "approved"
        reopened.close()

    asyncio.run(scenario())


def test_broker_over_file_store_persists_approval(tmp_path) -> None:
    async def scenario() -> None:
        root = tmp_path / "store"
        store = ConfirmationFileStore(root, runtime_id="broker-1")
        broker = ConfirmationBroker(store, runtime_id="broker-1")
        task = asyncio.create_task(broker.request(_request("req-1")))
        await asyncio.sleep(0)

        await broker.submit(_response("req-1"))
        result = await task
        assert result.decision == "approve"
        broker.close()

        reopened = ConfirmationFileStore(root, runtime_id="broker-1")
        record = reopened.get("req-1")
        assert record is not None
        assert record.status == "approved"
        assert record.response is not None
        assert record.response.decision == "approve"
        reopened.close()

    asyncio.run(scenario())
