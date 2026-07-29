from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.evolution import (
    ActivationDecision,
    ActivationStore,
    ArtifactActivationError,
    PolicyActivationService,
    PolicyActivationRecord,
    PolicyArtifactStore,
    PolicyApprovalService,
    PolicyEvidenceStore,
    PolicyReplacement,
    PolicyReviewService,
    PolicySelectionStore,
)

from tests.evolution.test_policy_candidates import POLICY_SOURCE, write_candidate
from tests.evolution.test_policy_review_evidence import add_cases, write_trace


def reviewed(
    tmp_path: Path,
    *,
    complete: bool = True,
    policy_source: str = POLICY_SOURCE,
):
    candidate = write_candidate(
        tmp_path / "candidates",
        policy_source=policy_source,
    )
    add_cases(candidate)
    trace = tmp_path / "trace.jsonl"
    if complete:
        write_trace(trace)
    evidence_store = PolicyEvidenceStore(tmp_path / "reviews")
    evidence = PolicyReviewService(evidence_store, timeout=10).review(
        candidate,
        trace_path=trace if complete else None,
    )
    return evidence_store, evidence


def services(tmp_path: Path):
    evidence_store = PolicyEvidenceStore(tmp_path / "reviews")
    activations = ActivationStore(tmp_path / "activations.json")
    artifacts = PolicyArtifactStore(tmp_path / "artifacts")
    selections = PolicySelectionStore(tmp_path / "policy-selections.json")
    approvals = PolicyApprovalService(evidence_store, activations, artifacts)
    runtime = PolicyActivationService(activations, artifacts, selections)
    return evidence_store, activations, artifacts, selections, approvals, runtime


def test_passed_evidence_can_be_approved_without_becoming_active(
    tmp_path: Path,
) -> None:
    source_store, evidence = reviewed(tmp_path / "source")
    _, activations, artifacts, selections, approvals, runtime = services(
        tmp_path / "runtime"
    )
    artifacts.import_review_snapshot(source_store, evidence)

    record = approvals.approve(
        evidence,
        operator="tester",
        source_store=source_store,
    )

    assert record.decision is ActivationDecision.APPROVED
    assert record.metadata["review_id"] == evidence.review_id
    assert record.metadata["evidence_digest"] == evidence.evidence_digest
    assert activations.check(evidence.candidate).approved is True
    assert artifacts.path_for(evidence.candidate.digest).is_dir()
    assert selections.active_records() == ()
    assert runtime.active() == ()


def test_review_required_needs_explicit_findings_acceptance_and_reason(
    tmp_path: Path,
) -> None:
    source_store, evidence = reviewed(tmp_path / "source", complete=False)
    _, _, _, _, approvals, _ = services(tmp_path / "runtime")

    with pytest.raises(ArtifactActivationError, match="accept"):
        approvals.approve(
            evidence,
            operator="tester",
            source_store=source_store,
        )
    with pytest.raises(ArtifactActivationError, match="reason"):
        approvals.approve(
            evidence,
            operator="tester",
            source_store=source_store,
            accept_findings=True,
        )

    record = approvals.approve(
        evidence,
        operator="tester",
        source_store=source_store,
        accept_findings=True,
        reason="Reviewed missing historical replay evidence",
    )

    assert record.metadata["accepted_findings"] is True
    assert record.reason is not None


def test_failed_evidence_cannot_be_approved_or_activated(tmp_path: Path) -> None:
    mismatched = POLICY_SOURCE.replace('name = "demo_policy"', 'name = "other"')
    source_store, evidence = reviewed(
        tmp_path / "source",
        policy_source=mismatched,
    )
    _, activations, artifacts, selections, approvals, runtime = services(
        tmp_path / "runtime"
    )

    assert evidence.status == "failed"
    with pytest.raises(ArtifactActivationError, match="failed"):
        approvals.approve(
            evidence,
            operator="tester",
            source_store=source_store,
        )
    denied = approvals.deny(
        evidence,
        operator="tester",
        reason="contract mismatch",
    )
    with pytest.raises(ArtifactActivationError, match="not approved"):
        runtime.activate(denied.record_id, operator="tester")
    assert activations.records()[-1].decision is ActivationDecision.DENIED
    assert selections.active_records() == ()
    assert artifacts.path_for(evidence.candidate.digest).exists() is False


def test_activate_deactivate_and_rollback_preserve_global_history(
    tmp_path: Path,
) -> None:
    first_store, first = reviewed(tmp_path / "first")
    _, activations, artifacts, selections, approvals, runtime = services(
        tmp_path / "runtime"
    )
    first_approval = approvals.approve(
        first,
        operator="tester",
        source_store=first_store,
    )
    first_active = runtime.activate(first_approval.record_id, operator="tester")

    second_source = POLICY_SOURCE.replace('version = "1.0.0"', 'version = "2.0.0"')
    second_path = tmp_path / "second"
    second_store, second = reviewed(second_path, policy_source=second_source)
    manifest_path = second_store.snapshot_path(second.candidate.digest) / "evopi-policy.json"
    # The reviewed helper intentionally keeps manifest v1; create a real v2 candidate instead.
    assert manifest_path.is_file()
    second_candidate = Path(second.candidate.source)
    manifest = json.loads((second_candidate / "evopi-policy.json").read_text(encoding="utf-8"))
    manifest["version"] = "2.0.0"
    (second_candidate / "evopi-policy.json").write_text(json.dumps(manifest), encoding="utf-8")
    second = PolicyReviewService(second_store, timeout=10).review(
        second_candidate,
        trace_path=second_path / "trace.jsonl",
    )
    second_approval = approvals.approve(
        second,
        operator="tester",
        source_store=second_store,
    )
    second_active = runtime.activate(second_approval.record_id, operator="tester")

    assert first_active.action == "activate"
    assert second_active.previous_approval_id == first_approval.record_id
    assert runtime.active()[0].approval.record_id == second_approval.record_id

    rollback = runtime.rollback("demo_policy", operator="tester")
    assert rollback.action == "rollback"
    assert runtime.active()[0].approval.record_id == first_approval.record_id

    runtime.deactivate("demo_policy", operator="tester", reason="paused")
    assert runtime.active() == ()
    assert PolicySelectionStore(selections.path).records()[-1].action == "deactivate"
    assert len(activations.records()) == 2


def test_explicit_replacement_is_bound_to_expected_runtime_digest(
    tmp_path: Path,
) -> None:
    source_store, evidence = reviewed(tmp_path / "source")
    _, _, _, _, approvals, runtime = services(tmp_path / "runtime")
    approval = approvals.approve(
        evidence,
        operator="tester",
        source_store=source_store,
    )
    replacement = PolicyReplacement(
        policy_name="shell_safety",
        expected_digest="a" * 64,
    )

    runtime.activate(
        approval.record_id,
        operator="tester",
        replacement=replacement,
    )
    active = runtime.active()[0]

    assert active.selection.replacement == replacement


def test_activation_store_reads_v2_and_writes_v3_metadata(tmp_path: Path) -> None:
    path = tmp_path / "activations.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "activations": [
                    {
                        "record_id": "a" * 32,
                        "candidate": {
                            "kind": "plugin",
                            "name": "legacy",
                            "version": "1",
                            "source": "legacy.py",
                            "risk_level": "high",
                            "digest": "b" * 64,
                            "metadata": {},
                        },
                        "decision": "approved",
                        "decided_by": "tester",
                        "decided_at": "2026-01-01T00:00:00+00:00",
                        "evidence": [],
                        "reason": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = ActivationStore(path)
    legacy = store.records()[0]

    store.add(
        candidate=legacy.candidate,
        decision=ActivationDecision.DENIED,
        decided_by="tester",
        metadata={"migration": True},
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["activations"][0]["metadata"] == {}
    assert payload["activations"][1]["metadata"] == {"migration": True}


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"policy_name": ""}, "name"),
        ({"operator": ""}, "operator"),
        ({"action": "unknown"}, "action"),
        ({"approval_record_id": None}, "approval"),
        ({"candidate_digest": None}, "digest"),
    ],
)
def test_activation_record_rejects_incomplete_active_state(
    changes: dict[str, object],
    match: str,
) -> None:
    arguments: dict[str, object] = {
        "policy_name": "demo_policy",
        "action": "activate",
        "operator": "tester",
        "approval_record_id": "a" * 32,
        "candidate_digest": "b" * 64,
    }
    arguments.update(changes)

    with pytest.raises(ValueError, match=match):
        PolicyActivationRecord(**arguments)


def test_deactivation_record_rejects_stale_approval_binding() -> None:
    with pytest.raises(ValueError, match="deactivate"):
        PolicyActivationRecord(
            policy_name="demo_policy",
            action="deactivate",
            operator="tester",
            approval_record_id="a" * 32,
            candidate_digest="b" * 64,
        )


def test_selection_store_rejects_unknown_action(tmp_path: Path) -> None:
    path = tmp_path / "policy-selections.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": [
                    {
                        "record_id": "a" * 32,
                        "policy_name": "demo_policy",
                        "action": "unknown",
                        "operator": "tester",
                        "approval_record_id": "b" * 32,
                        "candidate_digest": "c" * 64,
                        "previous_approval_id": None,
                        "replacement": None,
                        "reason": None,
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactActivationError, match="action"):
        PolicySelectionStore(path)
