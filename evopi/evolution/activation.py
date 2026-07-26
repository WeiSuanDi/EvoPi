"""Digest-bound activation records for evolvable runtime artifacts."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from uuid import uuid4

from evopi.policy.types import RiskLevel

ArtifactKind: TypeAlias = Literal["policy", "plugin"]
ACTIVATION_SCHEMA_VERSION = 2


class ActivationDecision(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"


class ArtifactActivationError(RuntimeError):
    """Raised when activation evidence is missing, corrupt, or inconsistent."""


@dataclass(slots=True, frozen=True, kw_only=True)
class ArtifactCandidate:
    kind: ArtifactKind
    name: str
    version: str
    source: str
    risk_level: RiskLevel
    digest: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValueError("artifact name and version must not be empty")
        digest = self.digest.lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("artifact digest must be a SHA-256 hexadecimal string")
        object.__setattr__(self, "digest", digest)


@dataclass(slots=True, frozen=True, kw_only=True)
class ActivationRecord:
    candidate: ArtifactCandidate
    decision: ActivationDecision
    decided_by: str
    record_id: str = field(default_factory=lambda: uuid4().hex)
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    evidence: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class ActivationCheck:
    record: ActivationRecord | None

    @property
    def approved(self) -> bool:
        return self.record is not None and self.record.decision is ActivationDecision.APPROVED


class ActivationStore:
    """Versioned, atomically persisted activation decisions."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser().resolve() if path is not None else None
        self._records: list[ActivationRecord] = []
        if self._path is not None and self._path.exists():
            self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    def records(self) -> tuple[ActivationRecord, ...]:
        return tuple(self._records)

    def add(
        self,
        *,
        candidate: ArtifactCandidate,
        decision: ActivationDecision,
        decided_by: str,
        evidence: tuple[str, ...] = (),
        reason: str | None = None,
        decided_at: datetime | None = None,
    ) -> ActivationRecord:
        if not decided_by.strip():
            raise ValueError("decided_by must not be empty")
        record = ActivationRecord(
            candidate=candidate,
            decision=decision,
            decided_by=decided_by,
            evidence=evidence,
            reason=reason,
            decided_at=decided_at or datetime.now(UTC),
        )
        self._records.append(record)
        try:
            self._save()
        except Exception:
            self._records.pop()
            raise
        return record

    def check(self, candidate: ArtifactCandidate) -> ActivationCheck:
        for record in reversed(self._records):
            item = record.candidate
            if (
                item.kind == candidate.kind
                and item.name == candidate.name
                and item.version == candidate.version
                and item.digest == candidate.digest
            ):
                return ActivationCheck(record=record)
        return ActivationCheck(record=None)

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "schema_version": ACTIVATION_SCHEMA_VERSION,
            "activations": [_record_to_dict(record) for record in self._records],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ArtifactActivationError(f"could not persist activation store: {exc}") from exc

    def _load(self) -> None:
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactActivationError(f"invalid activation store: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != ACTIVATION_SCHEMA_VERSION:
            raise ArtifactActivationError("unsupported activation store schema")
        items = raw.get("activations")
        if not isinstance(items, list):
            raise ArtifactActivationError("activations must be an array")
        self._records = [_record_from_dict(item) for item in items]


class ActivationGate:
    def __init__(self, store: ActivationStore) -> None:
        self.store = store

    def require(self, candidate: ArtifactCandidate) -> ActivationRecord:
        checked = self.store.check(candidate)
        if not checked.approved or checked.record is None:
            raise ArtifactActivationError(
                f"{candidate.kind} '{candidate.name}@{candidate.version}' is not approved "
                f"for digest {candidate.digest}"
            )
        return checked.record


def _record_to_dict(record: ActivationRecord) -> dict[str, Any]:
    candidate = asdict(record.candidate)
    return {
        "record_id": record.record_id,
        "candidate": candidate,
        "decision": record.decision.value,
        "decided_by": record.decided_by,
        "decided_at": record.decided_at.isoformat(),
        "evidence": list(record.evidence),
        "reason": record.reason,
    }


def _record_from_dict(raw: object) -> ActivationRecord:
    if not isinstance(raw, dict):
        raise ArtifactActivationError("activation record must be an object")
    candidate_raw = raw.get("candidate")
    if not isinstance(candidate_raw, dict):
        raise ArtifactActivationError("activation candidate must be an object")
    try:
        candidate = ArtifactCandidate(
            kind=cast(ArtifactKind, candidate_raw["kind"]),
            name=str(candidate_raw["name"]),
            version=str(candidate_raw["version"]),
            source=str(candidate_raw["source"]),
            risk_level=cast(RiskLevel, candidate_raw["risk_level"]),
            digest=str(candidate_raw["digest"]),
            metadata=dict(candidate_raw.get("metadata", {})),
        )
        decided_at = datetime.fromisoformat(str(raw["decided_at"]))
        if decided_at.tzinfo is None:
            raise ValueError("decided_at must include timezone")
        return ActivationRecord(
            record_id=str(raw["record_id"]),
            candidate=candidate,
            decision=ActivationDecision(str(raw["decision"])),
            decided_by=str(raw["decided_by"]),
            decided_at=decided_at.astimezone(UTC),
            evidence=tuple(str(item) for item in raw.get("evidence", [])),
            reason=str(raw["reason"]) if raw.get("reason") is not None else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactActivationError(f"invalid activation record: {exc}") from exc


__all__ = [
    "ACTIVATION_SCHEMA_VERSION",
    "ActivationCheck",
    "ActivationDecision",
    "ActivationGate",
    "ActivationRecord",
    "ActivationStore",
    "ArtifactActivationError",
    "ArtifactCandidate",
    "ArtifactKind",
]
