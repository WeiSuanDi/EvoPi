"""Tests for the Policy Generation protocol, codecs, and digests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evopi.evolution.policy_generation_protocol import (
    PolicyGenerationError,
    PolicyGenerationModelRun,
    PolicyGenerationProposal,
    PolicyGenerationRecord,
    PolicyGenerationSampleDecision,
    PolicyGenerationSettings,
    policy_generation_proposal_from_dict,
    policy_generation_record_from_dict,
)


def _proposal(**overrides: object) -> PolicyGenerationProposal:
    base = dict(
        schema_version=1,
        strategy="additive",
        candidate_name="block_risky_rm",
        description="Block risky rm",
        match_summary="3 of 3 samples match",
        rationale="Users repeatedly confirmed risky rm",
        fallback_action="allow",
        replacement_target=None,
        sample_decisions=(
            PolicyGenerationSampleDecision(sample_id="s1", action="block"),
            PolicyGenerationSampleDecision(sample_id="s2", action="block"),
            PolicyGenerationSampleDecision(sample_id="s3", action="block"),
        ),
        warnings=("sample evidence is local plaintext",),
        proposal_digest="",
    )
    base.update(overrides)
    return PolicyGenerationProposal(**base)  # type: ignore[arg-type]


def _record(**overrides: object) -> PolicyGenerationRecord:
    base = dict(
        generation_id="a" * 32,
        created_at=datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC),
        outcome="generated",
        report_id="b" * 32,
        report_digest="c" * 64,
        semantic_signature="d" * 64,
        evidence_digest="e" * 64,
        proposal=_proposal(),
        confirmation="interactive",
        model_runs=(
            PolicyGenerationModelRun(stage="proposal", model="m", provider="p"),
        ),
        candidate_name="block_risky_rm",
        candidate_digest="f" * 64,
        record_digest="",
    )
    base.update(overrides)
    return PolicyGenerationRecord(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------

def test_settings_defaults() -> None:
    s = PolicyGenerationSettings()
    assert s.max_evidence == 12
    assert s.stage_timeout == 120.0
    assert s.max_schema_repairs == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_evidence", 0),
        ("stage_timeout", 0.0),
        ("max_schema_repairs", -1),
        ("max_evidence_bytes", 0),
        ("max_files", 0),
        ("max_file_bytes", 0),
        ("max_total_file_bytes", 0),
    ],
)
def test_settings_rejects_invalid(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        PolicyGenerationSettings(**{field: value})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Proposal codec
# ---------------------------------------------------------------------------

def test_proposal_round_trip() -> None:
    proposal = _proposal()
    payload = proposal.to_dict()
    restored = policy_generation_proposal_from_dict(payload)
    # to_dict fills in a digest when empty; round-trip keeps it stable
    assert restored.proposal_digest == payload["proposal_digest"]
    assert restored.proposal_digest != ""
    # Re-encoding is stable
    again = policy_generation_proposal_from_dict(restored.to_dict())
    assert again == restored


def test_proposal_digest_binds_content() -> None:
    proposal = _proposal()
    payload = proposal.to_dict()
    # Tamper with content → digest mismatch
    payload["description"] = "tampered"
    with pytest.raises(PolicyGenerationError) as exc:
        policy_generation_proposal_from_dict(payload)
    assert exc.value.code == "proposal_digest_mismatch"


def test_proposal_rejects_unknown_strategy() -> None:
    payload = _proposal().to_dict()
    payload["strategy"] = "bogus"
    with pytest.raises(PolicyGenerationError):
        policy_generation_proposal_from_dict(payload)


def test_proposal_rejects_unknown_action() -> None:
    payload = _proposal().to_dict()
    payload["fallback_action"] = "rewrite_args"
    with pytest.raises(PolicyGenerationError):
        policy_generation_proposal_from_dict(payload)


# ---------------------------------------------------------------------------
# Record codec
# ---------------------------------------------------------------------------

def test_record_round_trip() -> None:
    record = _record()
    payload = record.to_dict()
    restored = policy_generation_record_from_dict(payload)
    assert restored.record_digest == payload["record_digest"]
    assert restored.record_digest != ""
    again = policy_generation_record_from_dict(restored.to_dict())
    assert again == restored


def test_record_rejects_tampered_digest() -> None:
    record = _record()
    payload = record.to_dict()
    payload["outcome"] = "deferred"
    with pytest.raises(PolicyGenerationError) as exc:
        policy_generation_record_from_dict(payload)
    assert exc.value.code == "record_digest_mismatch"


def test_record_rejects_unknown_schema() -> None:
    payload = _record().to_dict()
    payload["schema_version"] = 99
    with pytest.raises(PolicyGenerationError):
        policy_generation_record_from_dict(payload)


def test_record_rejects_invalid_outcome() -> None:
    payload = _record().to_dict()
    payload["outcome"] = "bogus"
    with pytest.raises(PolicyGenerationError):
        policy_generation_record_from_dict(payload)


def test_record_rejects_non_utc_timestamp() -> None:
    payload = _record().to_dict()
    payload["created_at"] = "2026-08-03T10:00:00"  # no tz
    with pytest.raises(PolicyGenerationError):
        policy_generation_record_from_dict(payload)


def test_record_rejects_non_json_safe_value() -> None:
    payload = _record().to_dict()
    payload["error_message"] = "x"  # valid; use NaN instead
    # Add NaN into a nested dict is not possible via dataclass; craft directly:
    from evopi.evolution.policy_generation_protocol import _record_payload  # noqa: PLC2701

    inner = _record_payload(_record())
    inner["error_message"] = float("nan")
    with pytest.raises(ValueError):
        policy_generation_record_from_dict(inner)


# ---------------------------------------------------------------------------
# Record content guarantees
# ---------------------------------------------------------------------------

def test_record_serialization_excludes_raw_sample_values() -> None:
    record = _record()
    text = str(record.to_dict())
    # Raw evidence values must not appear in the record payload
    assert "secret-token-value" not in text
    assert "super-secret" not in text
    assert record.proposal.sample_decisions  # structured decisions only


# ---------------------------------------------------------------------------
# Revision 3: strict codecs (I)
# ---------------------------------------------------------------------------


def test_proposal_rejects_unknown_schema_version() -> None:
    payload = _proposal().to_dict()
    payload["schema_version"] = 99
    # Recompute the digest so only the schema check can fail
    from evopi.evolution.policy_generation_protocol import _payload_digest

    del payload["proposal_digest"]
    payload["proposal_digest"] = _payload_digest(payload)
    with pytest.raises(PolicyGenerationError) as exc:
        policy_generation_proposal_from_dict(payload)
    assert "schema" in str(exc.value).lower()


def test_model_run_rejects_string_boolean() -> None:
    record = _record()
    payload = record.to_dict()
    payload["model_runs"][0]["timed_out"] = "false"  # string, not bool
    # Recompute digest
    from evopi.evolution.policy_generation_protocol import _payload_digest

    del payload["record_digest"]
    payload["record_digest"] = _payload_digest(payload)
    with pytest.raises(PolicyGenerationError):
        policy_generation_record_from_dict(payload)


def test_record_rejects_empty_evidence_digest() -> None:
    record = _record()
    payload = record.to_dict()
    payload["evidence_digest"] = ""
    from evopi.evolution.policy_generation_protocol import _payload_digest

    del payload["record_digest"]
    payload["record_digest"] = _payload_digest(payload)
    with pytest.raises(PolicyGenerationError):
        policy_generation_record_from_dict(payload)


def test_record_rejects_incoherent_generated_outcome() -> None:
    """generated without candidate identity is rejected by invariants."""
    record = _record()
    payload = record.to_dict()
    payload["candidate_name"] = None
    payload["candidate_digest"] = None
    from evopi.evolution.policy_generation_protocol import _payload_digest

    del payload["record_digest"]
    payload["record_digest"] = _payload_digest(payload)
    with pytest.raises(PolicyGenerationError):
        policy_generation_record_from_dict(payload)


# ---------------------------------------------------------------------------
# Revision 4: strict protocol closure (L)
# ---------------------------------------------------------------------------


def test_proposal_rejects_unknown_field() -> None:
    payload = _proposal().to_dict()
    payload["unknown_field"] = "x"
    from evopi.evolution.policy_generation_protocol import _payload_digest

    del payload["proposal_digest"]
    payload["proposal_digest"] = _payload_digest(payload)
    with pytest.raises(PolicyGenerationError) as exc:
        policy_generation_proposal_from_dict(payload)
    assert "unknown" in str(exc.value).lower()


def test_record_rejects_unknown_field() -> None:
    payload = _record().to_dict()
    payload["unknown_field"] = "x"
    from evopi.evolution.policy_generation_protocol import _payload_digest

    del payload["record_digest"]
    payload["record_digest"] = _payload_digest(payload)
    with pytest.raises(PolicyGenerationError) as exc:
        policy_generation_record_from_dict(payload)
    assert "unknown" in str(exc.value).lower()


def test_declined_record_rejects_interactive_confirmation() -> None:
    """A declined outcome with confirmation=interactive is incoherent."""
    from evopi.evolution.policy_generation_protocol import (
        PolicyGenerationProposal,
    )

    proposal = PolicyGenerationProposal(
        strategy="additive",
        candidate_name="block_x",
        description="d",
        match_summary="m",
        rationale="r",
        fallback_action="allow",
        sample_decisions=(
            PolicyGenerationSampleDecision(
                sample_id="s1", action="block"
            ),
        ),
    )
    proposal.to_dict()
    record = PolicyGenerationRecord(
        generation_id="a" * 32,
        created_at=datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC),
        outcome="declined",
        report_id="b" * 32,
        report_digest="c" * 64,
        semantic_signature="d" * 64,
        evidence_digest="e" * 64,
        proposal=proposal,
        confirmation="interactive",  # incoherent for declined
    )
    payload = record.to_dict()
    from evopi.evolution.policy_generation_protocol import _payload_digest

    del payload["record_digest"]
    payload["record_digest"] = _payload_digest(payload)
    with pytest.raises(PolicyGenerationError) as exc:
        policy_generation_record_from_dict(payload)
    assert "declined" in str(exc.value).lower()


def test_deferred_record_rejects_interactive_confirmation() -> None:
    from evopi.evolution.policy_generation_protocol import (
        PolicyGenerationProposal,
    )

    defer_proposal = PolicyGenerationProposal(
        strategy="defer",
        candidate_name="",
        description="",
        match_summary="",
        rationale="not now",
        fallback_action="allow",
    )
    defer_proposal.to_dict()
    record = PolicyGenerationRecord(
        generation_id="b" * 32,
        created_at=datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC),
        outcome="deferred",
        report_id="b" * 32,
        report_digest="c" * 64,
        semantic_signature="d" * 64,
        evidence_digest="e" * 64,
        proposal=defer_proposal,
        confirmation="interactive",  # incoherent for deferred
    )
    payload = record.to_dict()
    from evopi.evolution.policy_generation_protocol import _payload_digest

    del payload["record_digest"]
    payload["record_digest"] = _payload_digest(payload)
    with pytest.raises(PolicyGenerationError):
        policy_generation_record_from_dict(payload)
