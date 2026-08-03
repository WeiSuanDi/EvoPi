"""Tests for Policy Generation evidence reconstruction and selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.evolution import (
    PolicyOpportunityStore,
    discover_policy_opportunities,
)
from evopi.evolution.policy_generation_evidence import (
    PolicyGenerationEvidenceError,
    check_evidence_byte_budget,
    evidence_byte_size,
    load_discovery_report,
    load_policy_generation_evidence,
    resolve_policy_opportunity,
)
from evopi.evolution.policy_generation_protocol import (
    PolicyGenerationSettings,
)


# ---------------------------------------------------------------------------
# Trace fixtures (Discovery-compatible v2)
# ---------------------------------------------------------------------------


def _decision(policy_name: str, risk_level: str) -> dict[str, object]:
    return {
        "action": "require_confirmation",
        "reason": "test confirmation",
        "risk_level": risk_level,
        "rewritten_args": None,
        "replacement_result": None,
        "metadata": {},
        "policy_name": policy_name,
    }


def _confirmation_records(
    *,
    run_id: str,
    index: int,
    decision: str,
    command: str,
    created_at: str,
    policy_name: str = "tool_confirmation",
    risk_level: str = "medium",
    automatic: bool = False,
) -> list[dict[str, object]]:
    call_id = f"call-{index}"
    request_id = f"request-{index}"
    tool_call = {
        "id": call_id,
        "name": "shell_command",
        "arguments": {"command": command},
    }
    policy_decision = _decision(policy_name, risk_level)
    return [
        {
            "schema_version": 2,
            "type": "policy_evaluation",
            "run_id": run_id,
            "created_at": created_at,
            "data": {
                "hook": "before_tool_call",
                "input": {
                    "tool_call": tool_call,
                    "arguments": {"command": command},
                },
                "final": policy_decision,
                "decisions": [policy_decision],
            },
        },
        {
            "schema_version": 2,
            "type": "confirmation_request",
            "run_id": run_id,
            "created_at": created_at,
            "data": {
                "request": {
                    "id": request_id,
                    "hook": "before_tool_call",
                    "reason": "test confirmation",
                    "risk_level": risk_level,
                    "policy_names": [policy_name],
                    "tool_call": tool_call,
                    "arguments": {"command": command},
                }
            },
        },
        {
            "schema_version": 2,
            "type": "confirmation_response",
            "run_id": run_id,
            "created_at": created_at,
            "data": {
                "response": {
                    "request_id": request_id,
                    "decision": decision,
                    "reason": "human",
                    "metadata": {"automatic": automatic},
                }
            },
        },
    ]


def _write_trace(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _discover(tmp_path: Path) -> tuple[Path, object]:
    """Write a Trace with 4 denials + 2 approves and return (trace, report)."""
    trace = tmp_path / "trace.jsonl"
    records: list[dict[str, object]] = []
    created = "2026-08-03T10:00:00+00:00"
    for index in range(1, 7):
        decision = "deny" if index <= 4 else "approve"
        records.extend(
            _confirmation_records(
                run_id=f"run-{index}",
                index=index,
                decision=decision,
                command=f"dangerous-command-{index}",
                created_at=created,
            )
        )
    _write_trace(trace, records)
    report = discover_policy_opportunities([trace])
    return trace, report


# ---------------------------------------------------------------------------
# Opportunity resolution
# ---------------------------------------------------------------------------

def test_resolve_opportunity_by_full_signature(tmp_path: Path) -> None:
    _, report = _discover(tmp_path)
    opportunity = report.opportunities[0]
    resolved = resolve_policy_opportunity(report, opportunity.semantic_signature)
    assert resolved.semantic_signature == opportunity.semantic_signature


def test_resolve_opportunity_by_unique_prefix(tmp_path: Path) -> None:
    _, report = _discover(tmp_path)
    opportunity = report.opportunities[0]
    resolved = resolve_policy_opportunity(report, opportunity.semantic_signature[:12])
    assert resolved.semantic_signature == opportunity.semantic_signature


def test_resolve_opportunity_rejects_short_prefix(tmp_path: Path) -> None:
    _, report = _discover(tmp_path)
    opportunity = report.opportunities[0]
    with pytest.raises(PolicyGenerationEvidenceError):
        resolve_policy_opportunity(report, opportunity.semantic_signature[:4])


def test_resolve_opportunity_rejects_missing(tmp_path: Path) -> None:
    _, report = _discover(tmp_path)
    with pytest.raises(PolicyGenerationEvidenceError):
        resolve_policy_opportunity(report, "f" * 64)


# ---------------------------------------------------------------------------
# Evidence reconstruction
# ---------------------------------------------------------------------------

def test_load_evidence_selects_balanced_samples(tmp_path: Path) -> None:
    trace, report = _discover(tmp_path)
    opportunity = resolve_policy_opportunity(report, report.opportunities[0].semantic_signature)
    samples = load_policy_generation_evidence(report, opportunity, [trace])
    assert len(samples) == 6  # 4 deny + 2 approve, all within max_evidence
    decisions = [s.human_decision for s in samples]
    assert decisions.count("deny") == 4
    assert decisions.count("approve") == 2
    # Raw argument values preserved exactly
    assert any(s.arguments["command"] == "dangerous-command-1" for s in samples)


def test_load_evidence_respects_max_evidence(tmp_path: Path) -> None:
    trace, report = _discover(tmp_path)
    opportunity = resolve_policy_opportunity(report, report.opportunities[0].semantic_signature)
    settings = PolicyGenerationSettings(max_evidence=3)
    samples = load_policy_generation_evidence(report, opportunity, [trace], settings)
    assert len(samples) == 3
    decisions = [s.human_decision for s in samples]
    # Balanced: deny first, then approve
    assert decisions.count("deny") >= 1
    assert decisions.count("approve") >= 1


def test_load_evidence_skips_unreferenced_traces(tmp_path: Path) -> None:
    trace, report = _discover(tmp_path)
    opportunity = report.opportunities[0]
    # An unrelated trace file in a directory should be skipped
    unrelated = tmp_path / "trace-other.jsonl"
    _write_trace(unrelated, _confirmation_records(
        run_id="other-run",
        index=99,
        decision="deny",
        command="unrelated",
        created_at="2026-08-03T11:00:00+00:00",
    ))
    samples = load_policy_generation_evidence(report, opportunity, [tmp_path])
    assert len(samples) == 6
    assert all(s.trace_digest == _file_digest(trace) for s in samples)


def test_load_evidence_rejects_drifted_trace(tmp_path: Path) -> None:
    trace, report = _discover(tmp_path)
    opportunity = report.opportunities[0]
    # Tamper with the trace content → digest changes → no match
    text = trace.read_text(encoding="utf-8")
    trace.write_text(text.replace("dangerous-command-1", "changed-command-1"), encoding="utf-8")
    with pytest.raises(PolicyGenerationEvidenceError):
        load_policy_generation_evidence(report, opportunity, [trace])


# ---------------------------------------------------------------------------
# Byte budget
# ---------------------------------------------------------------------------

def test_evidence_byte_size_and_budget(tmp_path: Path) -> None:
    trace, report = _discover(tmp_path)
    opportunity = report.opportunities[0]
    samples = load_policy_generation_evidence(report, opportunity, [trace])
    size = evidence_byte_size(samples)
    assert size > 0
    check_evidence_byte_budget(samples, PolicyGenerationSettings())  # no raise


def test_evidence_byte_budget_fails_when_exceeded(tmp_path: Path) -> None:
    trace, report = _discover(tmp_path)
    opportunity = report.opportunities[0]
    samples = load_policy_generation_evidence(report, opportunity, [trace])
    settings = PolicyGenerationSettings(max_evidence_bytes=10)
    with pytest.raises(PolicyGenerationEvidenceError):
        check_evidence_byte_budget(samples, settings)


# ---------------------------------------------------------------------------
# Stored report loading
# ---------------------------------------------------------------------------

def test_load_discovery_report_round_trip(tmp_path: Path) -> None:
    trace, report = _discover(tmp_path)
    store = PolicyOpportunityStore(tmp_path / "store")
    stored = store.save(report)
    path = store.report_path(stored.report_id)
    loaded = load_discovery_report(path)
    assert loaded.report_id == stored.report_id


def _file_digest(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Revision 2: strict correlation and selection
# ---------------------------------------------------------------------------


def _write_raw_trace(path: Path, lines: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in lines:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _eval_record(run_id: str, line: int, call_id: str, command: str) -> dict:
    return {
        "schema_version": 2,
        "type": "policy_evaluation",
        "run_id": run_id,
        "created_at": "2026-08-03T10:00:00+00:00",
        "data": {
            "hook": "before_tool_call",
            "input": {
                "tool_call": {
                    "id": call_id,
                    "name": "shell_command",
                    "arguments": {"command": command},
                },
                "arguments": {"command": command},
            },
            "final": _decision("tool_confirmation", "medium"),
            "decisions": [_decision("tool_confirmation", "medium")],
        },
    }


def _req_record(run_id: str, line: int, request_id: str, call_id: str, command: str) -> dict:
    return {
        "schema_version": 2,
        "type": "confirmation_request",
        "run_id": run_id,
        "created_at": "2026-08-03T10:00:00+00:00",
        "data": {
            "request": {
                "id": request_id,
                "hook": "before_tool_call",
                "risk_level": "medium",
                "policy_names": ["tool_confirmation"],
                "tool_call": {
                    "id": call_id,
                    "name": "shell_command",
                    "arguments": {"command": command},
                },
                "arguments": {"command": command},
            }
        },
    }


def _resp_record(run_id: str, request_id: str, decision: str, automatic: bool = False) -> dict:
    return {
        "schema_version": 2,
        "type": "confirmation_response",
        "run_id": run_id,
        "created_at": "2026-08-03T10:00:00+00:00",
        "data": {
            "response": {
                "request_id": request_id,
                "decision": decision,
                "reason": "human",
                "metadata": {"automatic": automatic},
            }
        },
    }


def test_cross_run_response_reuse_fails(tmp_path: Path) -> None:
    """A response from a different Run must fail correlation."""
    trace = tmp_path / "trace.jsonl"
    _write_raw_trace(trace, [
        _eval_record("run-1", 1, "call-1", "dangerous-1"),
        _req_record("run-1", 2, "request-1", "call-1", "dangerous-1"),
        # Response references request-1 but from a different run
        _resp_record("run-2", "request-1", "deny"),
    ])
    # Discovery itself rejects the cross-Run correlation before generation.
    from evopi.evolution import PolicyDiscoveryError as DiscoveryError
    with pytest.raises(DiscoveryError):
        discover_policy_opportunities([trace])


def test_duplicate_request_id_fails(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_raw_trace(trace, [
        _eval_record("run-1", 1, "call-1", "dangerous-1"),
        _req_record("run-1", 2, "request-1", "call-1", "dangerous-1"),
        _req_record("run-1", 3, "request-1", "call-1", "dangerous-1"),
        _resp_record("run-1", "request-1", "deny"),
    ])
    from evopi.evolution import PolicyDiscoveryError as DiscoveryError
    with pytest.raises(DiscoveryError):
        discover_policy_opportunities([trace])


def test_wrong_tool_call_id_fails(tmp_path: Path) -> None:
    """Request referencing a different ToolCall ID must fail exact correlation."""
    trace = tmp_path / "trace.jsonl"
    _write_raw_trace(trace, [
        _eval_record("run-1", 1, "call-1", "dangerous-1"),
        _req_record("run-1", 2, "request-1", "call-999", "dangerous-1"),
        _resp_record("run-1", "request-1", "deny"),
    ])
    from evopi.evolution import PolicyDiscoveryError as DiscoveryError
    with pytest.raises(DiscoveryError):
        discover_policy_opportunities([trace])


def test_malformed_automatic_flag_fails(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    records = [
        _eval_record("run-1", 1, "call-1", "dangerous-1"),
        _req_record("run-1", 2, "request-1", "call-1", "dangerous-1"),
    ]
    resp = _resp_record("run-1", "request-1", "deny")
    resp["data"]["response"]["metadata"]["automatic"] = "yes"  # non-boolean
    records.append(resp)
    _write_raw_trace(trace, records)
    from evopi.evolution import PolicyDiscoveryError as DiscoveryError
    with pytest.raises(DiscoveryError):
        discover_policy_opportunities([trace])


def test_unsupported_schema_version_fails(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    records = [
        _eval_record("run-1", 1, "call-1", "dangerous-1"),
        _req_record("run-1", 2, "request-1", "call-1", "dangerous-1"),
        _resp_record("run-1", "request-1", "deny"),
    ]
    records[0]["schema_version"] = 99
    _write_raw_trace(trace, records)
    from evopi.evolution import PolicyDiscoveryError as DiscoveryError
    with pytest.raises(DiscoveryError):
        discover_policy_opportunities([trace])


def test_distinct_run_selection_prefers_new_run(tmp_path: Path) -> None:
    """Later evidence from a new Run beats an earlier sample from a covered Run."""
    trace = tmp_path / "trace.jsonl"
    records: list[dict] = []
    # 4 denies all in run-1
    for i in range(1, 5):
        records.append(_eval_record("run-1", i * 3 - 2, f"call-{i}", f"dangerous-{i}"))
        records.append(_req_record("run-1", i * 3 - 1, f"request-{i}", f"call-{i}", f"dangerous-{i}"))
        records.append(_resp_record("run-1", f"request-{i}", "deny"))
    # 1 deny in run-2 (later)
    records.append(_eval_record("run-2", 20, "call-20", "dangerous-20"))
    records.append(_req_record("run-2", 21, "request-20", "call-20", "dangerous-20"))
    records.append(_resp_record("run-2", "request-20", "deny"))
    _write_raw_trace(trace, records)
    report = discover_policy_opportunities([trace])
    opportunity = resolve_policy_opportunity(report, report.opportunities[0].semantic_signature)
    settings = PolicyGenerationSettings(max_evidence=4)
    samples = load_policy_generation_evidence(report, opportunity, [trace], settings)
    run_ids = [s.run_id for s in samples]
    assert run_ids.count("run-2") == 1  # new Run is represented before repeats
    assert run_ids.count("run-1") == 3


def test_unicode_byte_budget_is_utf8(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    records: list[dict] = []
    for i in range(1, 4):
        run_id = f"run-{i}"
        records.append(_eval_record(run_id, i * 3 - 2, f"call-{i}", f"危险命令-{i}"))
        records.append(_req_record(run_id, i * 3 - 1, f"request-{i}", f"call-{i}", f"危险命令-{i}"))
        records.append(_resp_record(run_id, f"request-{i}", "deny"))
    _write_raw_trace(trace, records)
    report = discover_policy_opportunities([trace])
    opportunity = resolve_policy_opportunity(report, report.opportunities[0].semantic_signature)
    samples = load_policy_generation_evidence(report, opportunity, [trace])
    size = evidence_byte_size(samples)
    assert size > 0
    # UTF-8 bytes exceed character count for multibyte content
    assert size >= len("危险命令-1") * 3


# ---------------------------------------------------------------------------
# Revision 3: unversioned/v1 Trace and generation-owned strict validation
# ---------------------------------------------------------------------------


def test_v1_trace_reconstructs_samples(tmp_path: Path) -> None:
    """Explicit schema v1 (and unversioned, treated as v1) reconstructs
    evidence through the public Generation loader."""
    trace = tmp_path / "trace.jsonl"
    records: list[dict] = []
    for i in range(1, 4):
        records.append(_eval_record(f"run-{i}", i * 4 - 3, f"call-{i}", f"dangerous-{i}"))
        records.append(_req_record(f"run-{i}", i * 4 - 2, f"request-{i}", f"call-{i}", f"dangerous-{i}"))
        records.append(_resp_record(f"run-{i}", f"request-{i}", "deny"))
    for record in records:
        record["schema_version"] = 1  # explicit v1
    _write_raw_trace(trace, records)
    report = discover_policy_opportunities([trace])
    assert len(report.opportunities) == 1
    opportunity = resolve_policy_opportunity(report, report.opportunities[0].semantic_signature)
    samples = load_policy_generation_evidence(report, opportunity, [trace])
    assert len(samples) == 3
    assert all(s.human_decision == "deny" for s in samples)
    assert all("dangerous" in str(s.arguments) for s in samples)


def test_cross_run_response_reuse_rejected_by_generation(tmp_path: Path) -> None:
    """Generation itself rejects cross-Run response reuse with line info.

    Builds a Discovery-valid corpus, then tampers only the response Run ID
    so the stored report still matches the original trace digest — the
    generation loader must detect the drift when re-reading the file.
    """
    trace = tmp_path / "trace.jsonl"
    records: list[dict] = []
    for i in range(1, 4):
        records.append(_eval_record(f"run-{i}", i * 4 - 3, f"call-{i}", f"dangerous-{i}"))
        records.append(_req_record(f"run-{i}", i * 4 - 2, f"request-{i}", f"call-{i}", f"dangerous-{i}"))
        records.append(_resp_record(f"run-{i}", f"request-{i}", "deny"))
    _write_raw_trace(trace, records)
    report = discover_policy_opportunities([trace])
    assert len(report.opportunities) == 1
    opportunity = resolve_policy_opportunity(report, report.opportunities[0].semantic_signature)

    # Tamper: point run-2's response at run-1's request ID (cross-Run reuse).
    lines = trace.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]
    parsed[5]["data"]["response"]["request_id"] = "request-1"
    with trace.open("w", encoding="utf-8") as handle:
        for record in parsed:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Rebind the immutable evidence references to the tampered corpus so this
    # reaches Generation's correlation validator instead of failing early on
    # a stale digest.
    from dataclasses import replace
    import hashlib

    rebound_digest = hashlib.sha256(trace.read_bytes()).hexdigest()
    rebound_opportunity = replace(
        opportunity,
        evidence=tuple(
            replace(item, trace_digest=rebound_digest)
            for item in opportunity.evidence
        ),
    )

    with pytest.raises(PolicyGenerationEvidenceError, match="run|Run") as captured:
        load_policy_generation_evidence(report, rebound_opportunity, [trace])
    assert captured.value.code == "invalid_trace"


# ---------------------------------------------------------------------------
# Revision 4: stream ordering (M2)
# ---------------------------------------------------------------------------


def test_response_before_request_rejected(tmp_path: Path) -> None:
    """Request must precede Response; reversed order is a fatal defect."""
    trace = tmp_path / "trace.jsonl"
    records = [
        _eval_record("run-1", 1, "call-1", "dangerous-1"),
        _req_record("run-1", 2, "request-1", "call-1", "dangerous-1"),
        _resp_record("run-1", "request-1", "deny"),
        _eval_record("run-2", 5, "call-2", "dangerous-2"),
        # Response BEFORE its request in run-2
        _resp_record("run-2", "request-2", "deny"),
        _req_record("run-2", 7, "request-2", "call-2", "dangerous-2"),
    ]
    _write_raw_trace(trace, records)
    from evopi.evolution import PolicyDiscoveryError as DiscoveryError

    with pytest.raises(DiscoveryError):
        discover_policy_opportunities([trace])


def test_unmatched_evaluation_at_eof_rejected(tmp_path: Path) -> None:
    """A confirmation-requiring Evaluation with no Request at EOF fails."""
    trace = tmp_path / "trace.jsonl"
    records = [
        _eval_record("run-1", 1, "call-1", "dangerous-1"),
        _req_record("run-1", 2, "request-1", "call-1", "dangerous-1"),
        _resp_record("run-1", "request-1", "deny"),
        _eval_record("run-2", 5, "call-2", "dangerous-2"),  # no request/response
    ]
    _write_raw_trace(trace, records)
    from evopi.evolution import PolicyDiscoveryError as DiscoveryError

    with pytest.raises(DiscoveryError):
        discover_policy_opportunities([trace])


def test_generation_loader_rejects_response_before_request_after_rebinding(
    tmp_path: Path,
) -> None:
    """Generation revalidates stream order even when evidence digests are rebound."""
    from dataclasses import replace
    import hashlib

    trace, report = _discover(tmp_path)
    opportunity = report.opportunities[0]
    records = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    records[1], records[2] = records[2], records[1]
    _write_raw_trace(trace, records)
    rebound_digest = hashlib.sha256(trace.read_bytes()).hexdigest()
    rebound_evidence = tuple(
        replace(
            item,
            trace_digest=rebound_digest,
            line_number=3 if item.line_number == 2 else item.line_number,
        )
        for item in opportunity.evidence
    )
    rebound_opportunity = replace(opportunity, evidence=rebound_evidence)

    with pytest.raises(
        PolicyGenerationEvidenceError,
        match="unknown request|precede|order",
    ):
        load_policy_generation_evidence(report, rebound_opportunity, [trace])
