"""Failing-first tests for InMemoryConfirmationStore and ConfirmationFileStore."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

import pytest

from evopi.core.tool import ToolCall
from evopi.harness.confirmation import (
    ConfirmationConflictError,
    ConfirmationDuplicateRequestError,
    ConfirmationError,
    ConfirmationExpiredError,
    ConfirmationFormatError,
    ConfirmationLockError,
    ConfirmationOrphanedError,
    ConfirmationRecord,
    ConfirmationRequest,
    ConfirmationResponse,
    ConfirmationStaleRevisionError,
    ConfirmationStoreClosedError,
    ConfirmationTransition,
    ConfirmationUnknownRequestError,
)
from evopi.harness.confirmation_store import (
    ConfirmationFileStore,
    InMemoryConfirmationStore,
)


def _request(request_id: str, **overrides: object) -> ConfirmationRequest:
    base: dict[str, object] = dict(hook="before_tool_call", reason="requires approval")
    base.update(overrides)
    return ConfirmationRequest(
        hook=base["hook"],  # type: ignore[arg-type]
        reason=base["reason"],  # type: ignore[arg-type]
        id=request_id,
    )


def _record(
    request_id: str,
    *,
    runtime_id: str = "runtime-1",
    status: str = "pending",
    revision: int = 1,
    response: ConfirmationResponse | None = None,
) -> ConfirmationRecord:
    return ConfirmationRecord(
        request=_request(request_id),
        status=status,  # type: ignore[arg-type]
        runtime_id=runtime_id,
        revision=revision,
        response=response,
        updated_at=datetime.now(UTC),
    )


def _response(request_id: str, decision: str = "approve") -> ConfirmationResponse:
    return ConfirmationResponse(
        request_id=request_id,
        decision=decision,  # type: ignore[arg-type]
        reason="ok",
        metadata={"source": "test"},
    )


def _expired_response(request_id: str) -> ConfirmationResponse:
    return ConfirmationResponse(
        request_id=request_id,
        decision="deny",
        reason="timed out",
        metadata={"automatic": True, "expired": True},
    )


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


def test_memory_create_get_list_pending() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.create(_record("req-b"))

    assert store.get("req-a") is not None
    assert store.get("req-a").request.id == "req-a"  # type: ignore[union-attr]
    assert store.get("missing") is None
    assert [r.request.id for r in store.list_pending()] == ["req-a", "req-b"]


def test_memory_duplicate_create_rejected() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    with pytest.raises(ConfirmationDuplicateRequestError):
        store.create(_record("req-a"))


def test_memory_transition_applies_optimistic_revision() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a", revision=1))
    response = _response("req-a")

    updated = store.transition(
        "req-a", expected_revision=1, status="approved", response=response
    )

    assert updated.status == "approved"
    assert updated.revision == 2
    # Defensive snapshots: equal but not the caller's object.
    assert updated.response == response
    assert updated.response is not response
    assert updated.updated_at.tzinfo is UTC
    assert store.get("req-a").revision == 2  # type: ignore[union-attr]


def test_memory_transition_unknown_rejected() -> None:
    store = InMemoryConfirmationStore()
    with pytest.raises(ConfirmationUnknownRequestError):
        store.transition(
            "missing", expected_revision=1, status="approved", response=None
        )


def test_memory_transition_stale_revision_rejected() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.transition(
        "req-a", expected_revision=1, status="approved", response=_response("req-a")
    )

    with pytest.raises(ConfirmationStaleRevisionError):
        store.transition(
            "req-a", expected_revision=1, status="denied", response=None
        )


def test_memory_transition_on_terminal_record_rejected() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.transition(
        "req-a", expected_revision=1, status="approved", response=_response("req-a")
    )

    with pytest.raises(ConfirmationConflictError, match="already approved"):
        store.transition("req-a", expected_revision=2, status="denied", response=None)


def test_memory_transition_to_pending_rejected() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    with pytest.raises(ConfirmationConflictError, match="not a terminal"):
        store.transition("req-a", expected_revision=1, status="pending", response=None)


def test_memory_transition_response_correlation_enforced() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    with pytest.raises(ConfirmationFormatError, match="does not correlate"):
        store.transition(
            "req-a",
            expected_revision=1,
            status="approved",
            response=_response("req-b"),
        )


def test_memory_expired_record_raises_expired() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.transition(
        "req-a",
        expected_revision=1,
        status="expired",
        response=_expired_response("req-a"),
    )

    with pytest.raises(ConfirmationExpiredError):
        store.transition("req-a", expected_revision=2, status="approved", response=None)


def test_memory_orphaned_record_raises_orphaned() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.transition("req-a", expected_revision=1, status="orphaned", response=None)

    with pytest.raises(ConfirmationOrphanedError):
        store.transition("req-a", expected_revision=2, status="approved", response=None)


def test_memory_batch_applies_all_or_nothing() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.create(_record("req-b"))
    store.transition(
        "req-b", expected_revision=1, status="approved", response=_response("req-b")
    )

    batch = (
        ConfirmationTransition(
            request_id="req-a",
            expected_revision=1,
            status="approved",
            response=_response("req-a"),
        ),
        ConfirmationTransition(
            request_id="req-b",
            expected_revision=1,
            status="denied",
            response=_response("req-b", decision="deny"),
        ),
    )
    with pytest.raises(ConfirmationStaleRevisionError):
        store.transition_batch(batch)

    assert store.get("req-a").status == "pending"  # type: ignore[union-attr]
    assert store.get("req-b").status == "approved"  # type: ignore[union-attr]


def test_memory_batch_duplicate_ids_rejected_before_any_transition() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.create(_record("req-b"))

    batch = (
        ConfirmationTransition(
            request_id="req-a",
            expected_revision=1,
            status="approved",
            response=_response("req-a"),
        ),
        ConfirmationTransition(
            request_id="req-a",
            expected_revision=1,
            status="denied",
            response=_response("req-a", decision="deny"),
        ),
    )
    with pytest.raises(ConfirmationDuplicateRequestError, match="in batch"):
        store.transition_batch(batch)

    assert store.get("req-a").status == "pending"  # type: ignore[union-attr]
    assert store.get("req-b").status == "pending"  # type: ignore[union-attr]


def test_memory_batch_success_returns_updated_records() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.create(_record("req-b"))

    updated = store.transition_batch(
        (
            ConfirmationTransition(
                request_id="req-a",
                expected_revision=1,
                status="approved",
                response=_response("req-a"),
            ),
            ConfirmationTransition(
                request_id="req-b",
                expected_revision=1,
                status="denied",
                response=_response("req-b", decision="deny"),
            ),
        )
    )

    assert [r.status for r in updated] == ["approved", "denied"]
    assert store.get("req-a").revision == 2  # type: ignore[union-attr]


def test_memory_recover_orphans_only_mutates_other_runtimes() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-other", runtime_id="runtime-other"))
    store.create(_record("req-own", runtime_id="runtime-own"))
    store.create(_record("req-done", runtime_id="runtime-other"))
    store.transition(
        "req-done",
        expected_revision=1,
        status="approved",
        response=_response("req-done"),
    )

    orphaned = store.recover_orphans(runtime_id="runtime-own")

    assert [r.request.id for r in orphaned] == ["req-other"]
    assert all(r.status == "orphaned" for r in orphaned)
    assert all(r.response is None for r in orphaned)
    assert store.get("req-own").status == "pending"  # type: ignore[union-attr]
    assert store.get("req-done").status == "approved"  # type: ignore[union-attr]
    assert store.get("req-other").status == "orphaned"  # type: ignore[union-attr]


def test_memory_close_is_idempotent_and_final() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.close()
    store.close()

    with pytest.raises(ConfirmationStoreClosedError):
        store.create(_record("req-b"))
    with pytest.raises(ConfirmationStoreClosedError):
        store.get("req-a")
    with pytest.raises(ConfirmationStoreClosedError):
        store.list_pending()
    with pytest.raises(ConfirmationStoreClosedError):
        store.transition(
            "req-a", expected_revision=1, status="approved", response=_response("req-a")
        )


# ---------------------------------------------------------------------------
# File store: persistence and lifecycle
# ---------------------------------------------------------------------------


def test_file_store_round_trip_after_reopen(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root, runtime_id="runtime-1")
    store.create(_record("req-a"))
    store.create(_record("req-b"))
    response = _response("req-b")
    store.transition("req-b", expected_revision=1, status="approved", response=response)
    store.close()

    # Terminal state survives; previously pending state is orphaned on reopen.
    reopened = ConfirmationFileStore(root, runtime_id="runtime-1")
    assert reopened.get("req-a").status == "orphaned"  # type: ignore[union-attr]
    assert reopened.get("req-a").response is None  # type: ignore[union-attr]
    assert reopened.get("req-b").status == "approved"  # type: ignore[union-attr]
    assert reopened.get("req-b").revision == 2  # type: ignore[union-attr]
    assert reopened.get("req-b").response.request_id == "req-b"  # type: ignore[union-attr]
    assert reopened.list_pending() == ()
    reopened.close()


def test_file_store_reopen_orphans_pending_even_with_same_runtime_id(tmp_path) -> None:
    # Reopening a lifetime-locked store is runtime recovery, never a host
    # reconnect: a reused runtime id must not reconstruct a waiter/tool.
    root = tmp_path / "store"
    store = ConfirmationFileStore(root, runtime_id="runtime-1")
    store.create(_record("req-a"))
    store.close()

    reopened = ConfirmationFileStore(root, runtime_id="runtime-1")
    record = reopened.get("req-a")
    assert record is not None
    assert record.status == "orphaned"
    assert record.response is None
    assert reopened.list_pending() == ()
    reopened.close()


def test_file_store_lock_conflict(tmp_path) -> None:
    root = tmp_path / "store"
    first = ConfirmationFileStore(root, runtime_id="runtime-1")

    with pytest.raises(ConfirmationLockError):
        ConfirmationFileStore(root, runtime_id="runtime-2")

    first.close()

    second = ConfirmationFileStore(root, runtime_id="runtime-2")
    second.close()


def test_file_store_closed_store_rejects_operations(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.close()

    with pytest.raises(ConfirmationStoreClosedError):
        store.create(_record("req-a"))
    with pytest.raises(ConfirmationStoreClosedError):
        store.get("req-a")
    store.close()  # idempotent


def test_file_store_duplicate_create_rejected(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.create(_record("req-a"))
    with pytest.raises(ConfirmationDuplicateRequestError):
        store.create(_record("req-a"))
    store.close()


def test_file_store_stale_revision_leaves_log_unchanged(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.create(_record("req-a"))
    store.transition(
        "req-a", expected_revision=1, status="approved", response=_response("req-a")
    )

    with pytest.raises(ConfirmationStaleRevisionError):
        store.transition("req-a", expected_revision=1, status="denied", response=None)

    facts = (root / "confirmation_facts.jsonl").read_text(encoding="utf-8")
    assert facts.count("denied") == 0
    store.close()


# ---------------------------------------------------------------------------
# File store: corruption and torn-write repair
# ---------------------------------------------------------------------------


def test_file_store_middle_corruption_fails_closed(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root, runtime_id="runtime-1")
    store.create(_record("req-a"))
    store.create(_record("req-b"))
    store.close()

    path = root / "confirmation_facts.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "{corrupt middle line}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ConfirmationFormatError, match="corrupt"):
        ConfirmationFileStore(root)

    # The failed open must release the lock for the next owner.
    path.write_text("\n".join(lines[:1] + lines[2:]), encoding="utf-8")
    recovered = ConfirmationFileStore(root, runtime_id="runtime-1")
    assert recovered.get("req-a") is not None
    assert recovered.get("req-b") is not None
    recovered.close()


def test_file_store_torn_tail_is_repaired_with_warning(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root, runtime_id="runtime-1")
    store.create(_record("req-a"))
    store.close()

    path = root / "confirmation_facts.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version":1,"fact":"torn')

    repaired = ConfirmationFileStore(root, runtime_id="runtime-1")
    assert repaired.repair_warnings
    assert "torn tail" in repaired.repair_warnings[0]
    # The pending record is still replayed; the reopen sweep then orphans it.
    assert repaired.get("req-a") is not None
    assert repaired.get("req-a").status == "orphaned"  # type: ignore[union-attr]

    # The partial line must have been truncated from the log.
    remaining = path.read_text(encoding="utf-8")
    assert remaining.endswith("\n")
    assert '"fact":"torn' not in remaining
    repaired.close()


def test_file_store_unknown_fact_type_fails_closed(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.create(_record("req-a"))
    store.close()

    path = root / "confirmation_facts.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"schema_version":1,"fact":"delete","data":{},'
            f'"created_at":"{datetime.now(UTC).isoformat()}"}}\n'
        )

    with pytest.raises(ConfirmationFormatError, match="unknown fact type"):
        ConfirmationFileStore(root)


def test_file_store_unknown_envelope_field_fails_closed(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.create(_record("req-a"))
    store.close()

    path = root / "confirmation_facts.jsonl"
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    envelope = json.loads(first_line)
    envelope["extra"] = True
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope) + "\n")

    with pytest.raises(ConfirmationFormatError, match="unknown field"):
        ConfirmationFileStore(root)


def test_file_store_unknown_schema_version_fails_closed(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.close()

    path = root / "confirmation_facts.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"schema_version":2,"fact":"create","data":{},'
            f'"created_at":"{datetime.now(UTC).isoformat()}"}}\n'
        )

    with pytest.raises(ConfirmationFormatError, match="schema version"):
        ConfirmationFileStore(root)


def test_file_store_duplicate_create_fact_fails_closed(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.create(_record("req-a"))
    store.close()

    path = root / "confirmation_facts.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(path.read_text(encoding="utf-8"))

    with pytest.raises(ConfirmationFormatError, match="duplicate create"):
        ConfirmationFileStore(root)


# ---------------------------------------------------------------------------
# File store: atomic batch and durability
# ---------------------------------------------------------------------------


def test_file_store_batch_rollback_persists_nothing(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root, runtime_id="runtime-1")
    store.create(_record("req-a"))
    store.create(_record("req-b"))

    batch = (
        ConfirmationTransition(
            request_id="req-a",
            expected_revision=1,
            status="approved",
            response=_response("req-a"),
        ),
        ConfirmationTransition(
            request_id="req-b",
            expected_revision=1,
            status="approved",
            response=_response("req-b"),
        ),
        ConfirmationTransition(
            request_id="req-missing",
            expected_revision=1,
            status="denied",
            response=_response("req-missing", decision="deny"),
        ),
    )
    with pytest.raises(ConfirmationUnknownRequestError):
        store.transition_batch(batch)

    # Nothing was persisted and no waiter state changed in memory.
    facts = (root / "confirmation_facts.jsonl").read_text(encoding="utf-8")
    assert "batch_transition" not in facts
    assert store.get("req-a").status == "pending"  # type: ignore[union-attr]
    assert store.get("req-b").status == "pending"  # type: ignore[union-attr]
    store.close()

    # Reopen orphans (never approves) the still-pending records, proving the
    # batch had no durable effect.
    reopened = ConfirmationFileStore(root, runtime_id="runtime-1")
    assert reopened.get("req-a").status == "orphaned"  # type: ignore[union-attr]
    assert reopened.get("req-b").status == "orphaned"  # type: ignore[union-attr]
    reopened.close()


def test_file_store_batch_writes_one_fact_line(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.create(_record("req-a"))
    store.create(_record("req-b"))
    store.transition(
        "req-a",
        expected_revision=1,
        status="approved",
        response=_response("req-a"),
    )
    store.transition_batch(
        (
            ConfirmationTransition(
                request_id="req-b",
                expected_revision=1,
                status="approved",
                response=_response("req-b"),
            ),
        )
    )
    store.close()

    facts = (root / "confirmation_facts.jsonl").read_text(encoding="utf-8").splitlines()
    assert sum("batch_transition" in line for line in facts) == 1
    assert sum('"fact":"transition"' in line for line in facts) == 1
    # Two creates, one single transition, one atomic batch fact.
    assert len(facts) == 4


def test_file_store_orphan_sweep_on_open(tmp_path) -> None:
    root = tmp_path / "store"
    crashed = ConfirmationFileStore(root, runtime_id="runtime-crashed")
    crashed.create(_record("req-orphan", runtime_id="runtime-crashed"))
    crashed.close()

    successor = ConfirmationFileStore(root, runtime_id="runtime-successor")
    record = successor.get("req-orphan")
    assert record is not None
    assert record.status == "orphaned"
    assert record.response is None
    assert successor.list_pending() == ()
    successor.close()


def test_file_store_explicit_recover_orphans_persists(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root, runtime_id="runtime-owner")
    store.create(_record("req-other", runtime_id="runtime-owner"))
    orphaned = store.recover_orphans(runtime_id="runtime-1")
    store.close()

    assert [r.request.id for r in orphaned] == ["req-other"]
    assert all(r.status == "orphaned" for r in orphaned)
    assert all(r.response is None for r in orphaned)

    reopened = ConfirmationFileStore(root, runtime_id="runtime-owner")
    record = reopened.get("req-other")
    assert record is not None
    assert record.status == "orphaned"
    assert record.response is None
    reopened.close()


def test_file_store_write_failure_raises_without_mutating_memory(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)

    def _fail_fsync(_fileno: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", _fail_fsync)

    with pytest.raises(ConfirmationError, match="failed to persist"):
        store.create(_record("req-a"))

    assert store.get("req-a") is None
    assert store.list_pending() == ()
    store.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_file_store_private_permissions(tmp_path) -> None:
    import stat

    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.create(_record("req-a"))
    store.close()

    assert stat.S_IMODE((root / "confirmation_facts.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "lock").stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Finding A (rev 2): terminal state and response semantics on the Store side
# ---------------------------------------------------------------------------


def test_create_rejects_non_revision_one_record() -> None:
    store = InMemoryConfirmationStore()
    with pytest.raises(ConfirmationFormatError, match="revision-1"):
        store.create(_record("req-a", revision=2))


def test_create_rejects_non_pending_record() -> None:
    store = InMemoryConfirmationStore()
    with pytest.raises(ConfirmationFormatError, match="pending"):
        store.create(_record("req-a", status="approved"))


def test_create_rejects_record_with_response() -> None:
    store = InMemoryConfirmationStore()
    with pytest.raises(ConfirmationFormatError, match="without a response"):
        store.create(_record("req-a", response=_response("req-a")))


def test_transition_approved_requires_approve_response() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    with pytest.raises(ConfirmationFormatError, match="requires decision 'approve'"):
        store.transition(
            "req-a",
            expected_revision=1,
            status="approved",
            response=_response("req-a", decision="deny"),
        )


def test_transition_expired_requires_deny_automatic_metadata() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    with pytest.raises(ConfirmationFormatError, match="deny decision"):
        store.transition(
            "req-a",
            expected_revision=1,
            status="expired",
            response=_response("req-a"),
        )
    with pytest.raises(ConfirmationFormatError, match="metadata.automatic"):
        store.transition(
            "req-a",
            expected_revision=1,
            status="expired",
            response=ConfirmationResponse(
                request_id="req-a",
                decision="deny",
                metadata={"expired": True},
            ),
        )


def test_transition_orphaned_requires_no_response() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    with pytest.raises(ConfirmationFormatError, match="must not carry a response"):
        store.transition(
            "req-a",
            expected_revision=1,
            status="orphaned",
            response=_response("req-a"),
        )


def test_crafted_fact_cannot_bypass_store_invariants(tmp_path) -> None:
    # A fact file asserting approved-with-deny must fail replay: a crafted
    # fact cannot bypass the public Store checks.
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.close()

    iso = datetime.now(UTC).isoformat()
    transition = json.dumps(
        {
            "schema_version": 1,
            "request_id": "req-a",
            "expected_revision": 1,
            "status": "approved",
            "response": {
                "schema_version": 1,
                "request_id": "req-a",
                "decision": "deny",
                "reason": "crafted",
                "metadata": {},
            },
        },
        separators=(",", ":"),
    )
    with (root / "confirmation_facts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(f'{{"schema_version":1,"fact":"transition","data":{transition},"created_at":"{iso}"}}\n')

    with pytest.raises(ConfirmationFormatError, match="requires decision"):
        ConfirmationFileStore(root)


# ---------------------------------------------------------------------------
# Finding B (rev 2): stores own defensive snapshots
# ---------------------------------------------------------------------------


def _mutable_request(request_id: str) -> ConfirmationRequest:
    request = _request(request_id)
    request.metadata = {"source": "original"}
    request.arguments = {"value": "original"}
    request.tool_call = ToolCall(
        id="call-1", name="echo", arguments={"value": "original"}
    )
    return request


def test_memory_create_defends_against_request_mutation() -> None:
    store = InMemoryConfirmationStore()
    request = _mutable_request("req-a")
    store.create(
        ConfirmationRecord(
            request=request,
            status="pending",
            runtime_id="runtime-1",
            revision=1,
            updated_at=datetime.now(UTC),
        )
    )

    request.id = "req-mutated"
    request.metadata["source"] = "mutated"
    request.arguments["value"] = "mutated"
    request.tool_call.arguments["value"] = "mutated"
    request.tool_call.name = "mutated"

    stored = store.get("req-a")
    assert stored is not None
    assert stored.request.id == "req-a"
    assert stored.request.metadata == {"source": "original"}
    assert stored.request.arguments == {"value": "original"}
    assert stored.request.tool_call is not None
    assert stored.request.tool_call.name == "echo"
    assert stored.request.tool_call.arguments == {"value": "original"}
    assert store.get("req-mutated") is None
    assert [r.request.id for r in store.list_pending()] == ["req-a"]


def test_memory_transition_defends_against_response_mutation() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    response = _response("req-a")
    response.metadata = {"source": "original"}
    store.transition(
        "req-a", expected_revision=1, status="approved", response=response
    )

    response.metadata["source"] = "mutated"

    stored = store.get("req-a")
    assert stored is not None
    assert stored.response is not None
    assert stored.response.metadata == {"source": "original"}


def test_memory_returned_records_are_detached() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))

    got = store.get("req-a")
    assert got is not None
    got.request.id = "mutated-id"
    got.request.metadata["hack"] = True

    pending = store.list_pending()[0]
    pending.request.id = "mutated-pending"

    transitioned = store.transition(
        "req-a", expected_revision=1, status="approved", response=_response("req-a")
    )
    transitioned.request.id = "mutated-transition"
    transitioned.response.metadata["hack"] = True  # type: ignore[union-attr]

    stored = store.get("req-a")
    assert stored is not None
    assert stored.request.id == "req-a"
    assert stored.request.metadata == {}
    assert stored.response is not None
    assert stored.response.metadata == {"source": "test"}
    assert store.get("mutated-id") is None


def test_memory_orphan_results_are_detached() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-other", runtime_id="runtime-other"))
    orphaned = store.recover_orphans(runtime_id="runtime-own")[0]
    orphaned.request.id = "mutated-orphan"

    assert store.get("req-other") is not None
    assert store.get("mutated-orphan") is None


def test_file_store_snapshots_requests_and_responses(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root, runtime_id="runtime-1")

    request = _mutable_request("req-a")
    store.create(
        ConfirmationRecord(
            request=request,
            status="pending",
            runtime_id="runtime-1",
            revision=1,
            updated_at=datetime.now(UTC),
        )
    )
    response = _response("req-a")
    response.metadata = {"source": "original"}
    store.transition(
        "req-a", expected_revision=1, status="approved", response=response
    )

    request.id = "req-mutated"
    request.metadata["source"] = "mutated"
    request.tool_call.arguments["value"] = "mutated"
    response.metadata["source"] = "mutated"
    store.close()

    reopened = ConfirmationFileStore(root, runtime_id="runtime-1")
    stored = reopened.get("req-a")
    assert stored is not None
    assert stored.request.id == "req-a"
    assert stored.request.metadata == {"source": "original"}
    assert stored.request.tool_call is not None
    assert stored.request.tool_call.arguments == {"value": "original"}
    assert stored.response is not None
    assert stored.response.metadata == {"source": "original"}
    reopened.close()


# ---------------------------------------------------------------------------
# Finding C (rev 2): durable corruption is not a torn tail
# ---------------------------------------------------------------------------


def test_file_store_newline_terminated_malformed_fact_fails_closed(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.create(_record("req-a"))
    store.close()

    with (root / "confirmation_facts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{malformed complete line}\n")

    with pytest.raises(ConfirmationFormatError, match="corrupt"):
        ConfirmationFileStore(root)


def test_file_store_invalid_utf8_fails_closed(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.close()

    path = root / "confirmation_facts.jsonl"
    path.write_bytes(b'{"schema_version":1}\xff\n')

    with pytest.raises(ConfirmationFormatError, match="UTF-8"):
        ConfirmationFileStore(root)

    # The failed open releases the lock for the next owner.
    path.write_bytes(b"")
    recovered = ConfirmationFileStore(root)
    recovered.close()


def test_file_store_duplicate_json_keys_fail_closed(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.close()

    iso = datetime.now(UTC).isoformat()
    with (root / "confirmation_facts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            '{"schema_version":1,"schema_version":1,"fact":"create",'
            f'"data":{{}},"created_at":"{iso}"}}\n'
        )

    with pytest.raises(ConfirmationFormatError, match="duplicate"):
        ConfirmationFileStore(root)


def test_file_store_nan_constant_fails_closed(tmp_path) -> None:
    root = tmp_path / "store"
    store = ConfirmationFileStore(root)
    store.close()

    iso = datetime.now(UTC).isoformat()
    with (root / "confirmation_facts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            '{"schema_version":1,"fact":"create","data":NaN,'
            f'"created_at":"{iso}"}}\n'
        )

    with pytest.raises(ConfirmationFormatError, match="corrupt"):
        ConfirmationFileStore(root)
