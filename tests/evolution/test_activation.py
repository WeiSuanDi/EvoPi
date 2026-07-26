from __future__ import annotations

from pathlib import Path

from evopi.evolution import (
    ActivationDecision,
    ActivationStore,
    ArtifactCandidate,
)


def candidate(path: Path, digest: str = "a" * 64) -> ArtifactCandidate:
    return ArtifactCandidate(
        kind="plugin",
        name="demo",
        version="1.0.0",
        source=str(path),
        risk_level="high",
        digest=digest,
    )


def test_activation_records_are_bound_to_candidate_digest(tmp_path: Path) -> None:
    store = ActivationStore(tmp_path / "activations.json")
    approved = candidate(tmp_path / "demo.py")

    record = store.add(
        candidate=approved,
        decision=ActivationDecision.APPROVED,
        decided_by="tester",
        evidence=("review.json",),
    )

    assert store.check(approved).approved is True
    assert store.check(candidate(tmp_path / "demo.py", "b" * 64)).approved is False
    assert record.candidate.digest == "a" * 64


def test_activation_store_round_trips_versioned_json(tmp_path: Path) -> None:
    path = tmp_path / "activations.json"
    store = ActivationStore(path)
    store.add(
        candidate=candidate(tmp_path / "demo.py"),
        decision=ActivationDecision.DENIED,
        decided_by="tester",
        reason="unsafe",
    )

    loaded = ActivationStore(path)

    assert loaded.records()[0].decision is ActivationDecision.DENIED
    assert loaded.records()[0].reason == "unsafe"
