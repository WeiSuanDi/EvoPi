from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.evolution import (
    PolicyCandidateError,
    PolicyCandidateSnapshotStore,
    inspect_policy_candidate,
)


POLICY_SOURCE = """\
from evopi.policy.decisions import PolicyDecision


class DemoPolicy:
    name = "demo_policy"
    version = "1.0.0"
    description = "A candidate Policy"
    hooks = ("before_tool_call",)
    priority = 50
    enabled = True
    source = "project"
    risk_level = "medium"
    metadata = {}

    def run(self, context):
        return PolicyDecision(action="allow", reason="candidate")


POLICY = DemoPolicy()
"""


def write_candidate(
    root: Path,
    *,
    policy_source: str = POLICY_SOURCE,
    entrypoint: str = "policy.py:POLICY",
) -> Path:
    candidate = root / "demo-policy"
    candidate.mkdir(parents=True)
    (candidate / "policy.py").write_text(policy_source, encoding="utf-8")
    (candidate / "evopi-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "demo_policy",
                "version": "1.0.0",
                "description": "A candidate Policy",
                "entrypoint": entrypoint,
                "hooks": ["before_tool_call"],
                "priority": 50,
                "source": "project",
                "risk_level": "medium",
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    return candidate


def test_static_inspection_does_not_execute_candidate_code(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        + POLICY_SOURCE
    )
    candidate_path = write_candidate(tmp_path, policy_source=source)

    report = inspect_policy_candidate(candidate_path)

    assert report.passed is True
    assert marker.exists() is False
    assert report.candidate.manifest.name == "demo_policy"
    assert len(report.candidate.artifact.digest) == 64


def test_candidate_rejects_entrypoint_escape_and_invalid_python(tmp_path: Path) -> None:
    escaped = write_candidate(tmp_path / "escaped", entrypoint="../policy.py:POLICY")

    with pytest.raises(PolicyCandidateError, match="escapes"):
        inspect_policy_candidate(escaped)

    broken = write_candidate(
        tmp_path / "broken",
        policy_source="def broken(:\n",
    )
    report = inspect_policy_candidate(broken)

    assert report.passed is False
    assert any("valid UTF-8 Python" in error for error in report.errors)


def test_snapshot_store_is_content_addressed_and_detects_source_drift(
    tmp_path: Path,
) -> None:
    candidate_path = write_candidate(tmp_path / "source")
    candidate = inspect_policy_candidate(candidate_path).candidate
    store = PolicyCandidateSnapshotStore(tmp_path / "reviews")

    snapshot = store.freeze(candidate)
    (candidate_path / "policy.py").write_text(POLICY_SOURCE + "\n# changed\n", encoding="utf-8")

    assert snapshot == store.path_for(candidate.artifact.digest)
    assert (snapshot / "policy.py").read_text(encoding="utf-8") == POLICY_SOURCE
    with pytest.raises(PolicyCandidateError, match="changed after inspection"):
        store.freeze(candidate)

