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
from evopi.evolution.file_lock import EvolutionFileLock

ArtifactKind: TypeAlias = Literal["policy", "plugin"]
ACTIVATION_SCHEMA_VERSION = 3


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
        if self.kind not in {"policy", "plugin"}:
            raise ValueError("artifact kind must be policy or plugin")
        if not self.name.strip() or not self.version.strip() or not self.source.strip():
            raise ValueError("artifact name, version, and source must not be empty")
        if self.risk_level not in {"low", "medium", "high", "critical"}:
            raise ValueError("artifact risk_level is invalid")
        digest = self.digest.lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("artifact digest must be a SHA-256 hexadecimal string")
        object.__setattr__(self, "digest", digest)
        _require_json_object(self.metadata, "artifact metadata")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(slots=True, frozen=True, kw_only=True)
class ActivationRecord:
    candidate: ArtifactCandidate
    decision: ActivationDecision
    decided_by: str
    record_id: str = field(default_factory=lambda: uuid4().hex)
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    evidence: tuple[str, ...] = ()
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _is_hex(self.record_id, length=32):
            raise ValueError("activation record ID must be 32 lowercase hexadecimal characters")
        if not isinstance(self.decision, ActivationDecision):
            raise ValueError("activation decision is invalid")
        if not self.decided_by.strip():
            raise ValueError("activation operator must not be empty")
        if self.decided_at.tzinfo is None:
            raise ValueError("activation timestamp must include timezone")
        if any(not isinstance(item, str) or not item for item in self.evidence):
            raise ValueError("activation evidence must contain non-empty strings")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ValueError("activation reason must be a string or null")
        _require_json_object(self.metadata, "activation metadata")
        object.__setattr__(self, "decided_at", self.decided_at.astimezone(UTC))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(slots=True, frozen=True, kw_only=True)
class ActivationCheck:
    record: ActivationRecord | None

    @property
    def approved(self) -> bool:
        return self.record is not None and self.record.decision is ActivationDecision.APPROVED


class ActivationStore:
    """Versioned, atomically persisted activation decisions."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            self._path = None
        else:
            expanded = Path(path).expanduser()
            _reject_store_symlink(expanded)
            self._path = expanded.resolve()
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
        metadata: dict[str, Any] | None = None,
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
            metadata=dict(metadata or {}),
        )
        if self._path is None:
            self._records.append(record)
            return record
        with EvolutionFileLock(self._lock_path):
            if self._path.exists():
                self._load()
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

    def get(self, record_id: str) -> ActivationRecord:
        for record in self._records:
            if record.record_id == record_id:
                return record
        raise ArtifactActivationError(f"activation record does not exist: {record_id}")

    @property
    def _lock_path(self) -> Path:
        assert self._path is not None
        return self._path.with_name(f"{self._path.name}.lock")

    def _save(self) -> None:
        if self._path is None:
            return
        _reject_store_symlink(self._path)
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
        _reject_store_symlink(self._path)
        try:
            raw = json.loads(
                self._path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ArtifactActivationError(f"invalid activation store: {exc}") from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "activations"}
            or type(raw.get("schema_version")) is not int
            or raw["schema_version"] not in {2, ACTIVATION_SCHEMA_VERSION}
        ):
            raise ArtifactActivationError("unsupported activation store schema")
        items = raw.get("activations")
        if not isinstance(items, list):
            raise ArtifactActivationError("activations must be an array")
        records = [_record_from_dict(item, schema_version=raw["schema_version"]) for item in items]
        record_ids = [record.record_id for record in records]
        if len(record_ids) != len(set(record_ids)):
            raise ArtifactActivationError("duplicate activation record ID")
        self._records = records


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
        "metadata": record.metadata,
    }


def _record_from_dict(raw: object, *, schema_version: int) -> ActivationRecord:
    record_fields = {
        "record_id",
        "candidate",
        "decision",
        "decided_by",
        "decided_at",
        "evidence",
        "reason",
    }
    if schema_version == ACTIVATION_SCHEMA_VERSION:
        record_fields.add("metadata")
    if not isinstance(raw, dict) or set(raw) != record_fields:
        raise ArtifactActivationError("activation record must be an object")
    candidate_raw = raw.get("candidate")
    candidate_fields = {
        "kind",
        "name",
        "version",
        "source",
        "risk_level",
        "digest",
        "metadata",
    }
    if not isinstance(candidate_raw, dict) or set(candidate_raw) != candidate_fields:
        raise ArtifactActivationError("activation candidate must be an object")
    try:
        kind = _string(candidate_raw["kind"], "candidate kind")
        risk_level = _string(candidate_raw["risk_level"], "candidate risk level")
        evidence_raw = raw["evidence"]
        if not isinstance(evidence_raw, list) or any(
            not isinstance(item, str) for item in evidence_raw
        ):
            raise ValueError("evidence must be a string array")
        reason = raw["reason"]
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string or null")
        candidate_metadata = candidate_raw["metadata"]
        record_metadata = raw.get("metadata", {})
        if not isinstance(candidate_metadata, dict) or not isinstance(
            record_metadata, dict
        ):
            raise ValueError("activation metadata must be an object")
        candidate = ArtifactCandidate(
            kind=cast(ArtifactKind, kind),
            name=_string(candidate_raw["name"], "candidate name"),
            version=_string(candidate_raw["version"], "candidate version"),
            source=_string(candidate_raw["source"], "candidate source"),
            risk_level=cast(RiskLevel, risk_level),
            digest=_string(candidate_raw["digest"], "candidate digest"),
            metadata=dict(candidate_metadata),
        )
        decided_at = datetime.fromisoformat(_string(raw["decided_at"], "decided_at"))
        if decided_at.tzinfo is None:
            raise ValueError("decided_at must include timezone")
        return ActivationRecord(
            record_id=_string(raw["record_id"], "record ID"),
            candidate=candidate,
            decision=ActivationDecision(_string(raw["decision"], "decision")),
            decided_by=_string(raw["decided_by"], "decided_by"),
            decided_at=decided_at.astimezone(UTC),
            evidence=tuple(evidence_raw),
            reason=reason,
            metadata=dict(record_metadata),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactActivationError(f"invalid activation record: {exc}") from exc


def _require_json_safe(value: Any, label: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be strictly JSON-safe") from exc


def _require_json_object(value: object, label: str) -> None:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object with string keys")
    _require_json_safe(value, label)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _is_hex(value: object, *, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _reject_store_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ArtifactActivationError(f"refusing symbolic link activation store: {path}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


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
