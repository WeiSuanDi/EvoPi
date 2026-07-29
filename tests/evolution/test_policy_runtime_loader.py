from __future__ import annotations

from pathlib import Path

import pytest

from evopi.evolution import (
    ArtifactActivationError,
    PolicyArtifactLoader,
)

from tests.evolution.test_policy_activation_pipeline import reviewed, services


def _active_policy(tmp_path: Path):
    source_store, evidence = reviewed(tmp_path / "source")
    _, _, _, _, approvals, runtime = services(tmp_path / "runtime")
    approval = approvals.approve(
        evidence,
        operator="tester",
        source_store=source_store,
    )
    runtime.activate(approval.record_id, operator="tester")
    return runtime


def test_loader_revalidates_and_marks_active_policy_snapshot(tmp_path: Path) -> None:
    runtime = _active_policy(tmp_path)

    loaded = PolicyArtifactLoader().load_active(runtime)

    assert len(loaded) == 1
    artifact = loaded[0]
    assert artifact.policy.name == "demo_policy"
    assert artifact.policy.metadata["evolution_artifact_digest"] == artifact.digest
    assert (
        artifact.policy.metadata["evolution_activation_id"]
        == artifact.approval_record_id
    )
    assert (
        artifact.policy.metadata["evolution_selection_id"]
        == artifact.selection_record_id
    )


def test_loader_rejects_artifact_tampering_before_import(tmp_path: Path) -> None:
    runtime = _active_policy(tmp_path)
    active = runtime.active()[0]
    (active.artifact_path / "policy.py").write_text(
        "\n# changed after approval\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactActivationError, match="snapshot"):
        PolicyArtifactLoader().load_active(runtime)
