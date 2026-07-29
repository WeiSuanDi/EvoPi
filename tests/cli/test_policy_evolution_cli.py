from __future__ import annotations

import json
from pathlib import Path

from evopi.cli.main import main

from tests.evolution.test_policy_review_evidence import add_cases, write_trace
from tests.evolution.test_policy_candidates import write_candidate


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
