"""Vertical flow: generated candidate → formal Review Worker.

Fully mocked generation (scripted models), then the real isolated formal
Review Worker.  Asserts generated artifacts reach ``review_required`` —
never approval — and that Generation itself never imports the candidate.
"""

from __future__ import annotations

import json
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete
from evopi.evolution import (
    PolicyCandidateGenerationService,
    PolicyEvidenceStore,
    PolicyGenerationProposal,
    PolicyGenerationSampleDecision,
    PolicyReviewService,
    discover_policy_opportunities,
    resolve_evolution_home,
)
from evopi.evolution.policy_generation_evidence import (
    load_policy_generation_evidence,
    resolve_policy_opportunity,
)
from tests.evolution.test_policy_pattern_discovery import (
    _confirmation_records,
    _write_trace,
)


def _make_opportunity(tmp_path: Path) -> tuple[Path, object, object]:
    trace = tmp_path / "trace.jsonl"
    records: list[dict[str, object]] = []
    for index in range(1, 4):
        records.extend(
            _confirmation_records(
                run_id=f"run-{index}",
                index=index,
                decision="deny",
                command=f"risky-command-{index}",
                created_at=datetime(2026, 1, index, tzinfo=UTC),
            )
        )
    _write_trace(trace, records)
    report = discover_policy_opportunities([trace])
    opportunity = report.opportunities[0]
    return trace, report, opportunity


class _TwoStageModel:
    """Serves Proposal then Candidate; never contacts a Provider."""

    name = "scripted"
    provider = "test"

    def __init__(self, proposal_payload: dict, candidate_payload: dict) -> None:
        self._proposal = proposal_payload
        self._candidate = candidate_payload

    def stream(self, context):
        from evopi.core.messages import UserMessage

        async def _stream():
            stage = "proposal"
            for message in context.messages:
                if isinstance(message, UserMessage) and "PROPOSAL" in message.content:
                    stage = "candidate"
                    break
            payload = self._candidate if stage == "candidate" else self._proposal
            yield ModelComplete(
                message=AssistantMessage(
                    content=json.dumps(payload),
                    stop_reason="stop",
                ),
            )

        return _stream()


def _proposal_for(
    report: object,
    opportunity: object,
    *,
    strategy: str,
    candidate_name: str,
) -> PolicyGenerationProposal:
    ids = [
        f"{e.trace_digest[:8]}:{e.line_number}"
        for e in opportunity.evidence  # type: ignore[attr-defined]
    ]
    action = "block"  # both strategies tighten to block in these fixtures
    proposal = PolicyGenerationProposal(
        strategy=strategy,  # type: ignore[arg-type]
        candidate_name=candidate_name,
        description="Generated test policy",
        match_summary=f"{len(ids)}/{len(ids)}",
        rationale="evidence-bound",
        fallback_action="allow" if strategy == "additive" else "require_confirmation",
        replacement_target=(
            "tool_confirmation" if strategy == "replacement" else None
        ),
        sample_decisions=tuple(
            PolicyGenerationSampleDecision(sample_id=sid, action=action)
            for sid in ids
        ),
    )
    proposal.to_dict()
    return proposal


def _candidate_payload(
    candidate_name: str,
    *,
    metadata: dict | None = None,
    risk_level: str = "medium",
    action_on_risky: str = "block",
) -> dict:
    # Render metadata as a Python literal (JSON null is not valid Python).
    def _py_literal(value: object) -> object:
        if value is None:
            return "None"
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        raise TypeError(f"unsupported literal: {value!r}")

    if metadata:
        entries = ", ".join(
            f"{json.dumps(k)}: {_py_literal(v)}" for k, v in metadata.items()
        )
        metadata_literal = "{" + entries + "}"
    else:
        metadata_literal = "{}"
    return {
        "schema_version": 1,
        "files": [
            {
                "path": "policy.py",
                "content": (
                    "from __future__ import annotations\n"
                    "from evopi.policy.decisions import PolicyDecision\n"
                    "from evopi.policy.types import PolicyContext\n\n"
                    f"class GeneratedPolicy:\n"
                    f"    name = {json.dumps(candidate_name)}\n"
                    "    version = '0.1.0'\n"
                    "    description = 'Generated test policy'\n"
                    "    hooks = ('before_tool_call',)\n"
                    "    priority = 100\n"
                    "    enabled = True\n"
                    "    source = 'generated'\n"
                    f"    risk_level = {json.dumps(risk_level)}\n"
                    f"    metadata = {metadata_literal}\n\n"
                    "    def run(self, context: PolicyContext) -> PolicyDecision:\n"
                    "        if context.tool_call is not None and "
                    "'risky' in str(context.tool_call.arguments):\n"
                    f"            return PolicyDecision(action={json.dumps(action_on_risky)})\n"
                    "        return PolicyDecision(action='allow')\n\n"
                    "POLICY = GeneratedPolicy()\n"
                ),
            }
        ],
    }


@pytest.mark.parametrize(
    "strategy,candidate_name",
    [
        ("additive", "block_risky_command"),
        ("replacement", "tool_confirmation"),
    ],
)
def test_generated_candidate_formal_review_reaches_review_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
    candidate_name: str,
) -> None:
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    trace, report, opportunity = _make_opportunity(tmp_path)
    opportunity = resolve_policy_opportunity(
        report, opportunity.semantic_signature
    )
    evidence = load_policy_generation_evidence(report, opportunity, [trace])
    ids = [s.sample_id for s in evidence]

    proposal = _proposal_for(
        report,
        opportunity,
        strategy=strategy,
        candidate_name=candidate_name,
    )
    # Build the scripted model with correct sample ids
    if strategy == "additive":
        proposal_payload = {
            "schema_version": 1,
            "strategy": "additive",
            "candidate_name": candidate_name,
            "description": "Generated test policy",
            "match_summary": f"{len(ids)}/{len(ids)}",
            "rationale": "evidence-bound",
            "fallback_action": "allow",
            "replacement_target": None,
            "sample_decisions": [
                {"sample_id": sid, "action": "block"} for sid in ids
            ],
            "warnings": [],
        }
    else:
        proposal_payload = {
            "schema_version": 1,
            "strategy": "replacement",
            "candidate_name": "tool_confirmation",
            "description": "Generated test policy",
            "match_summary": f"{len(ids)}/{len(ids)}",
            "rationale": "evidence-bound",
            "fallback_action": "require_confirmation",
            "replacement_target": "tool_confirmation",
            # The replacement tightens historical require_confirmation to block,
            # so the formal Replay must report changed rather than unchanged.
            "sample_decisions": [
                {"sample_id": sid, "action": "block"} for sid in ids
            ],
            "warnings": [],
        }

    # Precompute the exact Host identity contract so the scripted model can
    # declare it in policy.py (mirrors what a real model reads from the prompt).
    proposal_digest = proposal.proposal_digest or proposal.to_dict()["proposal_digest"]
    from evopi.evolution.policy_generation import _evidence_digest

    evidence_digest = _evidence_digest(evidence)
    identity_metadata = {
        "generation_id": "a" * 32,
        "report_id": report.report_id,
        "report_digest": report.report_digest,
        "semantic_signature": opportunity.semantic_signature,
        "proposal_digest": proposal_digest,
        "strategy": strategy,
        "evidence_digest": evidence_digest,
        "replacement_target": (
            "tool_confirmation" if strategy == "replacement" else None
        ),
    }
    model = _TwoStageModel(
        proposal_payload,
        _candidate_payload(
            candidate_name,
            metadata=identity_metadata,
            risk_level="high" if strategy == "replacement" else "medium",
            action_on_risky="block",
        ),
    )
    service = PolicyCandidateGenerationService(model)
    target = tmp_path / "candidate"
    result = asyncio.run(
        service.materialize(
            proposal,
            report,
            opportunity,
            evidence,
            generation_id="a" * 32,
            path=target,
        )
    )
    assert result.candidate is not None
    assert (target / "policy.py").is_file()
    assert (target / "cases.json").is_file()
    assert (target / "cases.py").is_file()

    # Generation never imported the candidate: static inspection only.
    from evopi.evolution import inspect_policy_candidate

    inspection = inspect_policy_candidate(target)
    assert inspection.passed

    # Formal Review Worker (real isolated subprocess)
    store = PolicyEvidenceStore(resolve_evolution_home() / "review" / "policies")
    review_service = PolicyReviewService(store)
    evidence_record = review_service.review(target, trace_path=trace)
    report = evidence_record.supervisor_report
    assert report.status == "review_required", (
        f"expected review_required, got {report.status}: "
        f"{[(c.name, c.status) for c in report.checks]}"
    )
    # Replay check must exist and carry the expected case status:
    # additive -> new; replacement -> changed.
    replay_check = next(
        (c for c in report.checks if c.name == "trace_replay"),
        None,
    )
    assert replay_check is not None, f"no trace_replay check: {report.checks}"
    replay_meta = replay_check.metadata
    if strategy == "additive":
        assert replay_meta.get("new", 0) > 0, (
            f"additive replay should contain new cases: {replay_meta}"
        )
        assert replay_meta.get("changed", 0) == 0, (
            f"additive replay should not contain changed cases: {replay_meta}"
        )
    else:
        assert replay_meta.get("changed", 0) > 0, (
            f"replacement replay should contain changed cases: {replay_meta}"
        )
    # Generated-source warning present (via schema warning finding)
    assert any(
        "generated" in finding.message.lower()
        for finding in report.findings
    ), f"no generated-source finding in {report.findings}"
    # Never approved/activated
    assert report.status != "passed"
