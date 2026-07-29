from __future__ import annotations

import json
from pathlib import Path

from evopi.cli.main import (
    _policy_activation_service_from_args,
    build_parser,
    main,
)
from evopi.evolution import PolicyEvidenceStore, PolicyReviewService

from tests.evolution.test_policy_review_evidence import add_cases, write_trace
from tests.evolution.test_policy_candidates import write_candidate


def test_coding_cli_loads_global_active_policies_unless_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    enabled = build_parser().parse_args([])
    disabled = build_parser().parse_args(["--no-evolved-policies"])

    assert _policy_activation_service_from_args(enabled) is not None
    assert _policy_activation_service_from_args(disabled) is None


def test_policy_init_creates_inactive_candidate_directory(
    tmp_path: Path,
    capsys,
) -> None:
    target = tmp_path / "candidate"

    exit_code = main(
        [
            "policy",
            "init",
            "safe-shell",
            "--path",
            str(target),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "candidate"
    assert payload["name"] == "safe_shell"
    assert (target / "evopi-policy.json").is_file()
    assert (target / "policy.py").is_file()
    assert (target / "cases.py").is_file()
    assert "approve" not in payload["next"]


def test_formal_policy_review_saves_evidence_and_preserves_exit_codes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    candidate = write_candidate(tmp_path / "candidates")
    add_cases(candidate)
    trace = tmp_path / "trace.jsonl"
    write_trace(trace)
    home = tmp_path / "home"
    monkeypatch.setenv("EVOPI_HOME", str(home))

    exit_code = main(
        [
            "policy",
            "review",
            str(candidate),
            "--trace",
            str(trace),
            "--review-timeout",
            "10",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "passed"
    assert payload["review_id"]
    assert payload["candidate"]["digest"]
    assert payload["supervisor_report"]["status"] == "passed"
    assert (
        home
        / "reviews"
        / "policies"
        / "reports"
        / f"{payload['review_id']}.json"
    ).is_file()


def test_formal_review_rejects_external_dry_run_reference(
    tmp_path: Path,
    capsys,
) -> None:
    candidate = write_candidate(tmp_path / "candidates")

    exit_code = main(
        [
            "policy",
            "review",
            str(candidate),
            "--dry-run-cases",
            "external:CASES",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 1
    assert "manifest" in output.err.lower()


def test_policy_approval_activation_status_and_deactivation_are_separate(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EVOPI_HOME", str(home))
    candidate = write_candidate(tmp_path / "candidates")
    add_cases(candidate)
    trace = tmp_path / "trace.jsonl"
    write_trace(trace)
    evidence = PolicyReviewService(
        PolicyEvidenceStore(home / "reviews" / "policies"),
        timeout=10,
    ).review(candidate, trace_path=trace)

    approve_code = main(
        [
            "policy",
            "approve",
            evidence.review_id,
            "--operator",
            "tester",
            "--json",
        ]
    )
    approval = json.loads(capsys.readouterr().out)
    list_code = main(["policy", "list", "--json"])
    before_activation = json.loads(capsys.readouterr().out)

    activate_code = main(
        [
            "policy",
            "activate",
            approval["record_id"],
            "--operator",
            "tester",
            "--json",
        ]
    )
    activated = json.loads(capsys.readouterr().out)
    status_code = main(["policy", "status", "demo_policy", "--json"])
    status = json.loads(capsys.readouterr().out)

    deactivate_code = main(
        [
            "policy",
            "deactivate",
            "demo_policy",
            "--operator",
            "tester",
            "--reason",
            "paused",
            "--json",
        ]
    )
    deactivated = json.loads(capsys.readouterr().out)

    assert approve_code == list_code == activate_code == status_code == deactivate_code == 0
    assert approval["status"] == "approved"
    assert before_activation["active"] == []
    assert activated["status"] == "active"
    assert status["active"][0]["name"] == "demo_policy"
    assert status["active"][0]["digest"] == evidence.candidate.digest
    assert deactivated["status"] == "inactive"


def test_review_required_cli_approval_requires_acceptance_and_reason(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EVOPI_HOME", str(home))
    candidate = write_candidate(tmp_path / "candidates")
    add_cases(candidate)
    evidence = PolicyReviewService(
        PolicyEvidenceStore(home / "reviews" / "policies"),
        timeout=10,
    ).review(candidate)
    assert evidence.status == "review_required"

    rejected = main(
        ["policy", "approve", evidence.review_id, "--operator", "tester"]
    )
    rejected_output = capsys.readouterr()
    accepted = main(
        [
            "policy",
            "approve",
            evidence.review_id,
            "--operator",
            "tester",
            "--accept-findings",
            "--reason",
            "Accepted missing historical replay",
            "--json",
        ]
    )
    accepted_payload = json.loads(capsys.readouterr().out)

    assert rejected == 1
    assert "accept" in rejected_output.err.lower()
    assert accepted == 0
    assert accepted_payload["status"] == "approved"


def test_policy_activate_replacement_arguments_must_be_paired(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))

    code = main(
        [
            "policy",
            "activate",
            "a" * 32,
            "--replace",
            "shell_safety",
        ]
    )

    assert code == 1
    assert "expected-digest" in capsys.readouterr().err


def test_policy_lifecycle_missing_evidence_is_a_clean_cli_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))

    code = main(["policy", "approve", "a" * 32])

    assert code == 1
    assert "invalid review evidence" in capsys.readouterr().err.lower()
