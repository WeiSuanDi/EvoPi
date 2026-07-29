from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.evolution import (
    PolicyEvidenceError,
    PolicyEvidenceStore,
    PolicyReviewService,
)

from tests.evolution.test_policy_candidates import POLICY_SOURCE, write_candidate


CASES_SOURCE = """\
from evopi.core.context import AgentContext
from evopi.core.tool import ToolCall
from evopi.policy.types import PolicyContext

CASES = [
    PolicyContext(
        hook="before_tool_call",
        agent_context=AgentContext(),
        tool_call=ToolCall(
            id="case-1",
            name="shell_command",
            arguments={"command": "python -m pytest"},
        ),
        arguments={"command": "python -m pytest"},
    )
]
"""


def add_cases(candidate: Path) -> None:
    manifest_path = candidate / "evopi-policy.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dry_run_entrypoint"] = "cases.py:CASES"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (candidate / "cases.py").write_text(CASES_SOURCE, encoding="utf-8")


def write_trace(path: Path) -> None:
    decision = {
        "action": "allow",
        "reason": "historical",
        "risk_level": "low",
        "rewritten_args": None,
        "replacement_result": None,
        "metadata": {},
        "policy_name": "demo_policy",
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "type": "policy_evaluation",
                "run_id": "run",
                "data": {
                    "hook": "before_tool_call",
                    "input": {
                        "tool_call": {
                            "id": "trace-call",
                            "name": "shell_command",
                            "arguments": {"command": "python -m pytest"},
                        },
                        "arguments": {"command": "python -m pytest"},
                        "tool_result": None,
                        "error": None,
                        "aborted": False,
                        "metadata": {},
                    },
                    "final": decision,
                    "decisions": [decision],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_formal_review_runs_snapshot_in_worker_and_persists_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "worker-executed.txt"
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('worker', encoding='utf-8')\n"
        + POLICY_SOURCE
    )
    candidate = write_candidate(tmp_path / "candidates", policy_source=source)
    add_cases(candidate)
    trace = tmp_path / "trace.jsonl"
    write_trace(trace)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-worker")
    store = PolicyEvidenceStore(tmp_path / "evidence")

    evidence = PolicyReviewService(store, timeout=10).review(
        candidate,
        trace_path=trace,
    )

    assert marker.read_text(encoding="utf-8") == "worker"
    assert evidence.supervisor_report.status == "passed"
    assert evidence.trace_digest is not None
    assert store.load(evidence.review_id) == evidence
    assert store.snapshot_path(evidence.candidate.digest).is_dir()


def test_worker_environment_does_not_receive_provider_secret(tmp_path: Path) -> None:
    source = (
        "import os\n"
        + POLICY_SOURCE.replace(
            'return PolicyDecision(action="allow", reason="candidate")',
            'if os.getenv("OPENAI_API_KEY"):\n'
            '            raise RuntimeError("secret leaked into worker")\n'
            '        return PolicyDecision(action="allow", reason="clean")',
        )
    )
    candidate = write_candidate(tmp_path / "candidates", policy_source=source)
    add_cases(candidate)
    store = PolicyEvidenceStore(tmp_path / "evidence")

    evidence = PolicyReviewService(
        store,
        timeout=10,
        environment={"OPENAI_API_KEY": "secret"},
    ).review(candidate)

    assert evidence.supervisor_report.status == "review_required"
    dry_run = next(
        check
        for check in evidence.supervisor_report.checks
        if check.name == "dry_run"
    )
    assert dry_run.status == "passed"


def test_worker_timeout_becomes_failed_immutable_evidence(tmp_path: Path) -> None:
    candidate = write_candidate(
        tmp_path / "candidates",
        policy_source="while True:\n    pass\n" + POLICY_SOURCE,
    )
    store = PolicyEvidenceStore(tmp_path / "evidence")

    evidence = PolicyReviewService(store, timeout=0.1).review(candidate)

    assert evidence.supervisor_report.status == "failed"
    assert any(
        "timed out" in finding.message.lower()
        for finding in evidence.supervisor_report.findings
    )


def test_evidence_store_rejects_report_tampering(tmp_path: Path) -> None:
    candidate = write_candidate(tmp_path / "candidates")
    store = PolicyEvidenceStore(tmp_path / "evidence")
    evidence = PolicyReviewService(store, timeout=10).review(candidate)
    report_path = store.report_path(evidence.review_id)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["supervisor_report"]["status"] = "passed"
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyEvidenceError, match="digest"):
        store.load(evidence.review_id)
