from __future__ import annotations

import json
import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from evopi.evolution import (
    PolicyEvidenceError,
    PolicyEvidenceStore,
    PolicyReviewService,
)

from tests.evolution.test_policy_candidates import POLICY_SOURCE, write_candidate
from evopi.evolution.file_lock import EvolutionFileLock, EvolutionStoreLockError


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
        f"Path({str(marker)!r}).write_text('worker', encoding='utf-8')\n" + POLICY_SOURCE
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
    source = "import os\n" + POLICY_SOURCE.replace(
        'return PolicyDecision(action="allow", reason="candidate")',
        'if os.getenv("OPENAI_API_KEY"):\n'
        '            raise RuntimeError("secret leaked into worker")\n'
        '        return PolicyDecision(action="allow", reason="clean")',
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
    dry_run = next(check for check in evidence.supervisor_report.checks if check.name == "dry_run")
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
        "timed out" in finding.message.lower() for finding in evidence.supervisor_report.findings
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


def test_evidence_save_obeys_writer_lock(tmp_path: Path) -> None:
    candidate = write_candidate(tmp_path / "candidates")
    store = PolicyEvidenceStore(tmp_path / "evidence")
    evidence = PolicyReviewService(store, timeout=10).review(candidate)
    other = replace(evidence, review_id="a" * 32, evidence_digest="")

    with EvolutionFileLock(store.root / "reports" / ".evidence.lock"):
        with pytest.raises(EvolutionStoreLockError):
            store.save(other)
    assert not store.report_path(other.review_id).exists()
    assert store.load(evidence.review_id) == evidence


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "worker",
        "kind",
        "extra",
        "identity",
        "false_pass",
        "check_status",
        "missing_checks",
        "finding_severity",
        "schema_not_applicable",
        "replay_not_applicable",
    ],
)
def test_evidence_rejects_malformed_payload_with_recomputed_digest(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidate = write_candidate(tmp_path / "candidates")
    store = PolicyEvidenceStore(tmp_path / "evidence")
    evidence = PolicyReviewService(store, timeout=10).review(candidate)
    payload = evidence.to_dict()
    payload.pop("evidence_digest")
    if mutation == "schema":
        payload["schema_version"] = True
    elif mutation == "worker":
        payload["worker"]["isolated_process"] = "false"
    elif mutation == "kind":
        payload["candidate"]["kind"] = "plugin"
    elif mutation == "extra":
        payload["unexpected"] = "field"
    elif mutation == "identity":
        payload["supervisor_report"]["candidate"]["name"] = "another_policy"
    elif mutation == "false_pass":
        payload["status"] = payload["supervisor_report"]["status"] = "passed"
    elif mutation == "check_status":
        payload["supervisor_report"]["checks"][0]["status"] = "invented"
    elif mutation == "missing_checks":
        payload["supervisor_report"]["checks"] = []
    elif mutation == "schema_not_applicable":
        payload["supervisor_report"]["checks"][0]["status"] = "not_applicable"
    elif mutation == "replay_not_applicable":
        payload["supervisor_report"]["checks"][2]["status"] = "not_applicable"
    else:
        payload["supervisor_report"]["findings"][0]["severity"] = "invented"
    payload["evidence_digest"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    store.report_path(evidence.review_id).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyEvidenceError):
        store.load(evidence.review_id)


def test_evidence_rejects_duplicate_keys_even_when_digest_matches(tmp_path: Path) -> None:
    candidate = write_candidate(tmp_path / "candidates")
    store = PolicyEvidenceStore(tmp_path / "evidence")
    evidence = PolicyReviewService(store, timeout=10).review(candidate)
    path = store.report_path(evidence.review_id)
    content = path.read_text(encoding="utf-8")
    path.write_text(
        content.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1),
        encoding="utf-8",
    )

    with pytest.raises(PolicyEvidenceError, match="duplicate"):
        store.load(evidence.review_id)


def test_review_worker_consumes_the_exact_trace_bytes_bound_to_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = write_candidate(tmp_path / "candidates")
    add_cases(candidate)
    original = tmp_path / "trace.jsonl"
    write_trace(original)
    original_bytes = original.read_bytes()
    store = PolicyEvidenceStore(tmp_path / "evidence")
    service = PolicyReviewService(store, timeout=10)
    run_worker = service._run_worker
    observed: list[Path] = []

    def mutate_original(candidate, snapshot, trace):
        original.write_text("corrupted while review is running", encoding="utf-8")
        observed.append(trace)
        return run_worker(candidate, snapshot, trace)

    monkeypatch.setattr(service, "_run_worker", mutate_original)
    evidence = service.review(candidate, trace_path=original)

    assert evidence.status == "passed"
    assert evidence.trace_digest == hashlib.sha256(original_bytes).hexdigest()
    assert observed[0] != original
    assert not observed[0].exists()


@pytest.mark.parametrize("mutation", ["verdict", "identity"])
def test_invalid_worker_verdict_is_recorded_as_failed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    candidate = write_candidate(tmp_path / "candidates")
    store = PolicyEvidenceStore(tmp_path / "evidence")
    execute = subprocess.run

    def corrupt_response(*args, **kwargs):
        result = execute(*args, **kwargs)
        payload = json.loads(result.stdout)
        if mutation == "verdict":
            payload["report"]["status"] = "passed"
        else:
            payload["report"]["candidate"]["name"] = "another_policy"
        result.stdout = json.dumps(payload)
        return result

    monkeypatch.setattr(subprocess, "run", corrupt_response)
    evidence = PolicyReviewService(store, timeout=10).review(candidate)

    assert evidence.status == "failed"
    assert store.load(evidence.review_id) == evidence
    assert any(
        "invalid protocol response" in finding.message
        for finding in evidence.supervisor_report.findings
    )
