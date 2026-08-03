"""Tests for the immutable Policy Generation store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from evopi.evolution.policy_generation_protocol import (
    PolicyGenerationError,
    PolicyGenerationProposal,
    PolicyGenerationRecord,
)
from evopi.evolution.policy_generation_store import PolicyGenerationStore


def _record(generation_id: str | None = None) -> PolicyGenerationRecord:
    return PolicyGenerationRecord(
        generation_id=generation_id or "a" * 32,
        created_at=datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC),
        outcome="generated",
        report_id="b" * 32,
        report_digest="c" * 64,
        semantic_signature="d" * 64,
        evidence_digest="e" * 64,
        proposal=PolicyGenerationProposal(
            strategy="additive",
            candidate_name="safe_policy",
            description="desc",
            match_summary="summary",
            rationale="rationale",
            fallback_action="allow",
        ),
        confirmation="interactive",
        candidate_name="safe_policy",
        candidate_digest="f" * 64,
    )


def test_store_save_and_load_round_trip(tmp_path: Path) -> None:
    store = PolicyGenerationStore(tmp_path / "store")
    record = _record()
    saved = store.save(record)
    assert saved.generation_id == record.generation_id
    assert store.exists(record.generation_id)
    loaded = store.load(record.generation_id)
    assert loaded == saved


def test_store_immutable_id_rejects_different_content(tmp_path: Path) -> None:
    store = PolicyGenerationStore(tmp_path / "store")
    record = _record()
    store.save(record)
    tampered = _record()
    # Same ID but different content
    tampered = PolicyGenerationRecord(
        generation_id=record.generation_id,
        created_at=record.created_at,
        outcome="declined",
        report_id=record.report_id,
        report_digest=record.report_digest,
        semantic_signature=record.semantic_signature,
        evidence_digest=record.evidence_digest,
        proposal=record.proposal,
        confirmation="declined",
    )
    with pytest.raises(PolicyGenerationError):
        store.save(tampered)


def test_store_same_id_same_content_is_idempotent(tmp_path: Path) -> None:
    store = PolicyGenerationStore(tmp_path / "store")
    record = _record()
    store.save(record)
    again = store.save(record)  # same content → no error
    assert again == store.load(record.generation_id)


def test_store_rejects_tampered_file(tmp_path: Path) -> None:
    store = PolicyGenerationStore(tmp_path / "store")
    record = _record()
    store.save(record)
    path = store.record_path(record.generation_id)
    # Tamper with the stored content
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace('"outcome": "generated"', '"outcome": "deferred"'), encoding="utf-8")
    with pytest.raises(PolicyGenerationError):
        store.load(record.generation_id)


def test_store_rejects_invalid_id(tmp_path: Path) -> None:
    store = PolicyGenerationStore(tmp_path / "store")
    with pytest.raises(PolicyGenerationError):
        store.record_path("not-hex")
    with pytest.raises(PolicyGenerationError):
        store.load("short")


def test_store_list_ids(tmp_path: Path) -> None:
    store = PolicyGenerationStore(tmp_path / "store")
    assert store.list_ids() == ()
    r1 = _record(generation_id="1" * 32)
    r2 = _record(generation_id="2" * 32)
    store.save(r1)
    store.save(r2)
    ids = store.list_ids()
    assert len(ids) == 2
    assert "1" * 32 in ids
    assert "2" * 32 in ids


# ---------------------------------------------------------------------------
# Revision 2: outcome invariants and storage failure semantics
# ---------------------------------------------------------------------------

from evopi.evolution.policy_generation_protocol import (  # noqa: E402
    PolicyGenerationError as ProtocolError,
)


def _defer_proposal() -> PolicyGenerationProposal:
    return PolicyGenerationProposal(
        strategy="defer",
        candidate_name="",
        description="",
        match_summary="",
        rationale="not now",
        fallback_action="allow",
    )


def test_generated_record_requires_candidate_identity(tmp_path: Path) -> None:
    from evopi.evolution.policy_generation_protocol import (
        PolicyGenerationRecord,
    )

    record = PolicyGenerationRecord(
        generation_id="1" * 32,
        created_at=datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC),
        outcome="generated",
        report_id="b" * 32,
        report_digest="c" * 64,
        semantic_signature="d" * 64,
        evidence_digest="e" * 64,
        proposal=_record().proposal,
        confirmation="interactive",
        candidate_name=None,  # missing identity → invalid
        candidate_digest=None,
    )
    with pytest.raises(ProtocolError):
        # Save forces codec round-trip which enforces the invariant
        store = PolicyGenerationStore(tmp_path / "store")
        store.save(record)


def test_declined_record_forbids_candidate_identity(tmp_path: Path) -> None:
    from evopi.evolution.policy_generation_protocol import (
        PolicyGenerationRecord,
    )

    record = PolicyGenerationRecord(
        generation_id="2" * 32,
        created_at=datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC),
        outcome="declined",
        report_id="b" * 32,
        report_digest="c" * 64,
        semantic_signature="d" * 64,
        evidence_digest="e" * 64,
        proposal=_record().proposal,
        confirmation="declined",
        candidate_name="should_not_exist",
        candidate_digest="f" * 64,
    )
    with pytest.raises(ProtocolError):
        store = PolicyGenerationStore(tmp_path / "store")
        store.save(record)


def test_deferred_record_requires_defer_proposal(tmp_path: Path) -> None:
    from evopi.evolution.policy_generation_protocol import (
        PolicyGenerationRecord,
    )

    record = PolicyGenerationRecord(
        generation_id="3" * 32,
        created_at=datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC),
        outcome="deferred",
        report_id="b" * 32,
        report_digest="c" * 64,
        semantic_signature="d" * 64,
        evidence_digest="e" * 64,
        proposal=_record().proposal,  # additive proposal, not defer
        confirmation="none",
    )
    with pytest.raises(ProtocolError):
        store = PolicyGenerationStore(tmp_path / "store")
        store.save(record)


def test_failed_record_may_have_no_proposal(tmp_path: Path) -> None:
    from evopi.evolution.policy_generation_protocol import (
        PolicyGenerationRecord,
    )

    record = PolicyGenerationRecord(
        generation_id="4" * 32,
        created_at=datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC),
        outcome="failed",
        report_id="b" * 32,
        report_digest="c" * 64,
        semantic_signature="d" * 64,
        evidence_digest="e" * 64,
        proposal=None,  # pre-Proposal failure
        confirmation="none",
        error_code="model_failed",
        error_message="boom",
    )
    store = PolicyGenerationStore(tmp_path / "store")
    saved = store.save(record)
    assert saved.outcome == "failed"
    assert saved.proposal is None


# ---------------------------------------------------------------------------
# Revision 4: JSON-safe retry/failover metadata round-trip (K4)
# ---------------------------------------------------------------------------


def test_retry_metadata_json_safe_round_trip(tmp_path: Path) -> None:
    """Retry/failover metadata survives strict codec + store round-trip."""
    from evopi.evolution.policy_generation_protocol import (
        PolicyGenerationModelRun,
    )

    run = PolicyGenerationModelRun(
        stage="proposal",
        model="m",
        provider="p",
        attempt=2,
        schema_repair_count=1,
        metadata={
            "retry_failover_events": [
                {
                    "type": "model_retry_start",
                    "retry": 1,
                    "next_attempt": 2,
                    "attempt_info": {
                        "candidate_id": "fallback-1",
                        "provider": "p2",
                        "model": "m2",
                        "failure_domain_id": "dom2",
                    },
                    "source_attempt_info": None,
                }
            ]
        },
    )
    record = _record()
    from dataclasses import replace

    record = replace(record, model_runs=(run,))
    store = PolicyGenerationStore(tmp_path / "store")
    saved = store.save(record)
    loaded = store.load(saved.generation_id)
    assert loaded.model_runs[0].metadata["retry_failover_events"][0][
        "attempt_info"
    ]["candidate_id"] == "fallback-1"
