"""Failing-first tests for InMemoryConfirmationStore and ConfirmationFileStore."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

import pytest

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
    assert updated.response is response
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
    store.transition("req-a", expected_revision=1, status="approved", response=None)

    with pytest.raises(ConfirmationStaleRevisionError):
        store.transition(
            "req-a", expected_revision=1, status="denied", response=None
        )


def test_memory_transition_on_terminal_record_rejected() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.transition("req-a", expected_revision=1, status="approved", response=None)

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
    with pytest.raises(ConfirmationConflictError, match="does not correlate"):
        store.transition(
            "req-a",
            expected_revision=1,
            status="approved",
            response=_response("req-b"),
        )


def test_memory_expired_record_raises_expired() -> None:
    store = InMemoryConfirmationStore()
    store.create(_record("req-a"))
    store.transition("req-a", expected_revision=1, status="expired", response=None)

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
    store.transition("req-b", expected_revision=1, status="approved", response=None)

    batch = (
        ConfirmationTransition(
            request_id="req-a", expected_revision=1, status="approved", response=None
        ),
        ConfirmationTransition(
            request_id="req-b", expected_revision=1, status="denied", response=None
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
            request_id="req-a", expected_revision=1, status="approved", response=None
        ),
        ConfirmationTransition(
            request_id="req-a", expected_revision=1, status="denied", response=None
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
        "req-done", expected_revision=1, status="approved", response=None
    )

    orphaned = store.recover_orphans(runtime_id="runtime-own")

    assert [r.request.id for r in orphaned] == ["req-other"]
    assert all(r.status == "orphaned" for r in orphaned)
    assert all(r.response is None for r in orphaned)
    assert store.get("req-own").status == "pending"  # type: ignore[union-attr]
    assert store.get("req-done").status == "approved"  # type: ignore[union-attr]
    assert store.get("req-other").status == "orphaned"  # type: ignore[union-attr]


def test_memory_close_is_idempotent_noop() -> None:
    store = InMemoryConfirmationStore()
    store.close()
    store.close()
    store.create(_record("req-a"))
    assert store.get("req-a") is not None


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

    reopened = ConfirmationFileStore(root, runtime_id="runtime-1")
    assert reopened.get("req-a").status == "pending"  # type: ignore[union-attr]
    assert reopened.get("req-b").status == "approved"  # type: ignore[union-attr]
    assert reopened.get("req-b").revision == 2  # type: ignore[union-attr]
    assert reopened.get("req-b").response.request_id == "req-b"  # type: ignore[union-attr]
    assert [r.request.id for r in reopened.list_pending()] == ["req-a"]
    reopened.close()


def test_file_store_same_runtime_reopen_keeps_pending_alive(tmp_path) -> None:
    # An external host may reconnect while a pending confirmation stays alive.
    root = tmp_path / "store"
    store = ConfirmationFileStore(root, runtime_id="runtime-1")
    store.create(_record("req-a"))
    store.close()

    reopened = ConfirmationFileStore(root, runtime_id="runtime-1")
    assert reopened.get("req-a").status == "pending"  # type: ignore[union-attr]
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
    store.transition("req-a", expected_revision=1, status="approved", response=None)

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
    assert repaired.get("req-a") is not None
    assert repaired.get("req-a").status == "pending"  # type: ignore[union-attr]

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
    store.close()

    store = ConfirmationFileStore(root, runtime_id="runtime-1")
    batch = (
        ConfirmationTransition(
            request_id="req-a", expected_revision=1, status="approved", response=None
        ),
        ConfirmationTransition(
            request_id="req-b", expected_revision=1, status="approved", response=None
        ),
        ConfirmationTransition(
            request_id="req-missing", expected_revision=1, status="denied", response=None
        ),
    )
    with pytest.raises(ConfirmationUnknownRequestError):
        store.transition_batch(batch)
    store.close()

    reopened = ConfirmationFileStore(root, runtime_id="runtime-1")
    assert reopened.get("req-a").status == "pending"  # type: ignore[union-attr]
    assert reopened.get("req-b").status == "pending"  # type: ignore[union-attr]
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
    store.close()

    # The owner reconnects with its own runtime id, so no open-time sweep runs;
    # the explicit call targets records owned by another runtime.
    store = ConfirmationFileStore(root, runtime_id="runtime-owner")
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
