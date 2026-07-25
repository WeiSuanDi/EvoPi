"""Tests for ApprovalRecord, ApprovalStore, and Activation Gate."""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evopi.policy.approval import (
    ApprovalRecord,
    ApprovalRequiredError,
    ApprovalStore,
)


# ---------------------------------------------------------------------------
# ApprovalRecord
# ---------------------------------------------------------------------------

def test_approval_record_matches_same_name_and_version() -> None:
    record = ApprovalRecord(
        policy_name="shell_safety",
        policy_version="1.0",
        approved_by="admin",
        approved_at=datetime.now(UTC),
        decision="approved",
    )
    assert record.matches("shell_safety", "1.0")
    assert not record.matches("shell_safety", "2.0")
    assert not record.matches("other_policy", "1.0")


def test_approval_record_defaults() -> None:
    record = ApprovalRecord(
        policy_name="p",
        policy_version="1",
        approved_by="admin",
        approved_at=datetime.now(UTC),
    )
    assert record.decision == "approved"
    assert record.evidence == []
    assert record.reason is None


# ---------------------------------------------------------------------------
# ApprovalStore — in-memory mode (no path)
# ---------------------------------------------------------------------------

def test_store_defaults_to_warn_mode_when_no_path() -> None:
    store = ApprovalStore(None)
    assert store.mode == "warn"
    assert store.path is None


def test_store_add_and_check_approved() -> None:
    store = ApprovalStore(None)
    store.add(
        policy_name="test_policy",
        policy_version="1.0",
        approved_by="tester",
        decision="approved",
    )
    loaded = store.check("test_policy", "1.0")
    assert loaded.approved is True
    assert loaded.record is not None
    assert loaded.record.decision == "approved"


def test_store_add_and_check_denied() -> None:
    store = ApprovalStore(None)
    store.add(
        policy_name="bad_policy",
        policy_version="1.0",
        approved_by="tester",
        decision="denied",
        reason="Fails safety check",
    )
    loaded = store.check("bad_policy", "1.0")
    assert loaded.approved is False
    assert loaded.record is not None
    assert loaded.record.decision == "denied"
    assert loaded.record.reason == "Fails safety check"


def test_store_check_nonexistent_returns_unapproved() -> None:
    store = ApprovalStore(None)
    loaded = store.check("nonexistent", "1.0")
    assert loaded.approved is False
    assert loaded.record is None


def test_store_cannot_add_duplicate() -> None:
    store = ApprovalStore(None)
    store.add(policy_name="p", policy_version="1", approved_by="a")
    with pytest.raises(ApprovalRequiredError, match="already exists"):
        store.add(policy_name="p", policy_version="1", approved_by="b")


# ---------------------------------------------------------------------------
# Activation Gate — strict / warn / off modes
# ---------------------------------------------------------------------------

def test_strict_mode_raises_for_unapproved() -> None:
    store = ApprovalStore(None, mode="strict")
    loaded = store.check("p", "1.0")
    with pytest.raises(ApprovalRequiredError, match="has not been approved"):
        loaded.raise_if_required("p", "1.0")


def test_strict_mode_raises_for_denied() -> None:
    store = ApprovalStore(None, mode="strict")
    store.add(
        policy_name="p", policy_version="1.0",
        approved_by="tester", decision="denied",
    )
    loaded = store.check("p", "1.0")
    with pytest.raises(ApprovalRequiredError, match="explicitly denied"):
        loaded.raise_if_required("p", "1.0")


def test_strict_mode_allows_approved() -> None:
    store = ApprovalStore(None, mode="strict")
    store.add(
        policy_name="p", policy_version="1.0",
        approved_by="tester", decision="approved",
    )
    loaded = store.check("p", "1.0")
    loaded.raise_if_required("p", "1.0")  # does not raise


def test_warn_mode_does_not_raise_for_unapproved() -> None:
    store = ApprovalStore(None, mode="warn")
    loaded = store.check("p", "1.0")
    loaded.raise_if_required("p", "1.0")  # does not raise, only logs warning


def test_warn_mode_does_not_raise_for_denied() -> None:
    store = ApprovalStore(None, mode="warn")
    store.add(
        policy_name="p", policy_version="1.0",
        approved_by="tester", decision="denied",
    )
    loaded = store.check("p", "1.0")
    loaded.raise_if_required("p", "1.0")  # does not raise


def test_off_mode_never_raises() -> None:
    store = ApprovalStore(None, mode="off")
    loaded = store.check("p", "1.0")
    loaded.raise_if_required("p", "1.0")  # no-op


# ---------------------------------------------------------------------------
# ApprovalStore — persistence
# ---------------------------------------------------------------------------

def test_store_persists_to_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "approvals.json"
        store = ApprovalStore(path, mode="strict")
        store.add(
            policy_name="p", policy_version="1.0",
            approved_by="admin",
            decision="approved",
            reason="Looks good",
        )

        # Load in a new store
        store2 = ApprovalStore(path, mode="strict")
        loaded = store2.check("p", "1.0")
        assert loaded.approved is True
        assert loaded.record is not None
        assert loaded.record.approved_by == "admin"
        assert loaded.record.reason == "Looks good"


def test_store_handles_missing_file_gracefully() -> None:
    store = ApprovalStore(Path("/nonexistent/approvals.json"), mode="warn")
    loaded = store.check("p", "1.0")
    assert loaded.approved is False


def test_store_rejects_invalid_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.json"
        path.write_text("not json")
        with pytest.raises(ApprovalRequiredError, match="not valid JSON"):
            ApprovalStore(path)


def test_store_rejects_non_object_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(ApprovalRequiredError, match="JSON object"):
            ApprovalStore(path)


def test_store_rejects_non_list_approvals() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.json"
        path.write_text(json.dumps({"approvals": "not-a-list"}))
        with pytest.raises(ApprovalRequiredError, match="must be a list"):
            ApprovalStore(path)


def test_store_rejects_duplicate_in_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "approvals.json"
        path.write_text(json.dumps({
            "approvals": [
                {
                    "policy_name": "p",
                    "policy_version": "1.0",
                    "approved_by": "a",
                    "approved_at": "2026-07-25T00:00:00Z",
                },
                {
                    "policy_name": "p",
                    "policy_version": "1.0",
                    "approved_by": "b",
                    "approved_at": "2026-07-25T01:00:00Z",
                },
            ]
        }))
        with pytest.raises(ApprovalRequiredError, match="Duplicate approval"):
            ApprovalStore(path)


def test_store_rejects_missing_required_field() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "approvals.json"
        path.write_text(json.dumps({
            "approvals": [{"policy_name": "p"}]
        }))
        with pytest.raises(ApprovalRequiredError, match="Missing required field"):
            ApprovalStore(path)


def test_store_records_are_listed_in_order() -> None:
    store = ApprovalStore(None)
    store.add(policy_name="b", policy_version="1", approved_by="x")
    store.add(policy_name="a", policy_version="1", approved_by="y")
    names = [r.policy_name for r in store.records()]
    assert names == ["b", "a"]


def test_store_preserves_evidence_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "approvals.json"
        store = ApprovalStore(path)
        store.add(
            policy_name="p", policy_version="1.0",
            approved_by="admin",
            evidence=[".evopi/trace.jsonl", ".evopi/replay.jsonl"],
        )
        store2 = ApprovalStore(path)
        loaded = store2.check("p", "1.0")
        assert loaded.record is not None
        assert loaded.record.evidence == [".evopi/trace.jsonl", ".evopi/replay.jsonl"]
