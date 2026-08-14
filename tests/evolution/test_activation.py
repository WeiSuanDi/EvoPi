from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.evolution import (
    ActivationDecision,
    ActivationStore,
    ArtifactActivationError,
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


@pytest.mark.parametrize(
    "payload",
    (
        '{"schema_version":true,"activations":[]}',
        '{"schema_version":3,"schema_version":3,"activations":[]}',
        '{"schema_version":3,"activations":[],"unexpected":true}',
    ),
)
def test_activation_store_rejects_noncanonical_root(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "activations.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ArtifactActivationError, match="activation store|schema"):
        ActivationStore(path)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("kind", "command"),
        ("risk_level", "unknown"),
        ("name", 7),
    ),
)
def test_activation_store_rejects_invalid_candidate_contract(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "activations.json"
    store = ActivationStore(path)
    store.add(
        candidate=candidate(tmp_path / "demo.py"),
        decision=ActivationDecision.APPROVED,
        decided_by="tester",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["activations"][0]["candidate"][field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactActivationError, match="activation record"):
        ActivationStore(path)


def test_activation_store_rejects_duplicate_record_id(tmp_path: Path) -> None:
    path = tmp_path / "activations.json"
    store = ActivationStore(path)
    store.add(
        candidate=candidate(tmp_path / "demo.py"),
        decision=ActivationDecision.APPROVED,
        decided_by="tester",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["activations"].append(dict(payload["activations"][0]))
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactActivationError, match="duplicate"):
        ActivationStore(path)


def test_artifact_candidate_rejects_invalid_runtime_literals(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="kind"):
        ArtifactCandidate(
            kind="command",  # type: ignore[arg-type]
            name="demo",
            version="1.0.0",
            source=str(tmp_path),
            risk_level="high",
            digest="a" * 64,
        )
    with pytest.raises(ValueError, match="risk"):
        ArtifactCandidate(
            kind="plugin",
            name="demo",
            version="1.0.0",
            source=str(tmp_path),
            risk_level="unknown",  # type: ignore[arg-type]
            digest="a" * 64,
        )
