"""Human approval, global Policy selection and rollback services."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from uuid import uuid4

from evopi.evolution.activation import (
    ActivationDecision,
    ActivationRecord,
    ActivationStore,
    ArtifactActivationError,
    ArtifactCandidate,
)
from evopi.evolution.file_lock import EvolutionFileLock
from evopi.evolution.policy_candidates import inspect_policy_candidate
from evopi.evolution.policy_evidence import (
    PolicyEvidenceStore,
    PolicyReviewEvidence,
)

POLICY_SELECTION_SCHEMA_VERSION = 1
PolicyActivationAction: TypeAlias = Literal["activate", "deactivate", "rollback"]


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyReplacement:
    policy_name: str
    expected_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy_name, str) or not self.policy_name.strip():
            raise ValueError("replacement Policy name must not be empty")
        _validate_digest(self.expected_digest)
        object.__setattr__(self, "expected_digest", self.expected_digest.lower())


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyActivationRecord:
    policy_name: str
    action: PolicyActivationAction
    operator: str
    approval_record_id: str | None = None
    candidate_digest: str | None = None
    previous_approval_id: str | None = None
    replacement: PolicyReplacement | None = None
    reason: str | None = None
    record_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.policy_name, str) or not self.policy_name.strip():
            raise ValueError("Policy activation name must not be empty")
        if self.action not in {"activate", "deactivate", "rollback"}:
            raise ValueError(f"unsupported Policy activation action: {self.action}")
        if not isinstance(self.operator, str) or not self.operator.strip():
            raise ValueError("Policy activation operator must not be empty")
        if not _is_hex(self.record_id, length=32):
            raise ValueError(
                "Policy activation record ID must be 32 lowercase hexadecimal characters"
            )
        if not isinstance(self.created_at, datetime):
            raise ValueError("Policy activation timestamp must be a datetime")
        if self.created_at.tzinfo is None:
            raise ValueError("Policy activation timestamp must include timezone")
        for label, value in (
            ("approval record ID", self.approval_record_id),
            ("previous approval ID", self.previous_approval_id),
        ):
            if value is not None and not _is_hex(value, length=32):
                raise ValueError(
                    f"{label} must be 32 lowercase hexadecimal characters"
                )
        if self.reason is not None and not isinstance(self.reason, str):
            raise ValueError("Policy activation reason must be a string or null")
        if self.action in {"activate", "rollback"}:
            if self.approval_record_id is None:
                raise ValueError(f"{self.action} requires an approval record ID")
            if self.candidate_digest is None:
                raise ValueError(f"{self.action} requires a candidate digest")
            _validate_digest(self.candidate_digest)
            object.__setattr__(
                self,
                "candidate_digest",
                self.candidate_digest.lower(),
            )
        elif any(
            value is not None
            for value in (
                self.approval_record_id,
                self.candidate_digest,
                self.previous_approval_id,
                self.replacement,
            )
        ):
            raise ValueError("deactivate cannot retain an approval or replacement binding")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))


@dataclass(slots=True, frozen=True, kw_only=True)
class ActivePolicySelection:
    selection: PolicyActivationRecord
    approval: ActivationRecord
    artifact_path: Path


class PolicyArtifactStore:
    """Content-addressed immutable snapshots of approved Policy candidates."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def path_for(self, digest: str) -> Path:
        _validate_digest(digest)
        return self.root / digest

    def import_review_snapshot(
        self,
        source_store: PolicyEvidenceStore,
        evidence: PolicyReviewEvidence,
    ) -> Path:
        loaded = source_store.load(evidence.review_id)
        if loaded != evidence:
            raise ArtifactActivationError("review evidence does not match its store")
        source = source_store.snapshot_path(evidence.candidate.digest)
        candidate = inspect_policy_candidate(source).candidate
        if candidate.artifact.digest != evidence.candidate.digest:
            raise ArtifactActivationError("review snapshot digest does not match evidence")
        target = self.path_for(evidence.candidate.digest)
        if target.exists():
            self.validate(evidence.candidate)
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            shutil.copytree(source, temporary)
            copied = inspect_policy_candidate(temporary).candidate
            if copied.artifact.digest != evidence.candidate.digest:
                raise ArtifactActivationError(
                    "approved Policy snapshot failed digest verification"
                )
            os.replace(temporary, target)
        except (OSError, shutil.Error, ArtifactActivationError):
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return target

    def validate(self, candidate: ArtifactCandidate) -> Path:
        if candidate.kind != "policy":
            raise ArtifactActivationError("artifact is not a Policy")
        target = self.path_for(candidate.digest)
        try:
            snapshot = inspect_policy_candidate(target).candidate
        except Exception as exc:
            raise ArtifactActivationError(
                f"approved Policy snapshot is unavailable: {exc}"
            ) from exc
        if snapshot.artifact.digest != candidate.digest:
            raise ArtifactActivationError(
                "approved Policy snapshot digest does not match its activation"
            )
        return target


class PolicyApprovalService:
    def __init__(
        self,
        evidence_store: PolicyEvidenceStore,
        activation_store: ActivationStore,
        artifact_store: PolicyArtifactStore,
    ) -> None:
        self.evidence_store = evidence_store
        self.activation_store = activation_store
        self.artifact_store = artifact_store

    def approve(
        self,
        evidence: PolicyReviewEvidence,
        *,
        operator: str,
        source_store: PolicyEvidenceStore | None = None,
        accept_findings: bool = False,
        reason: str | None = None,
    ) -> ActivationRecord:
        source = source_store or self.evidence_store
        evidence = source.load(evidence.review_id)
        if evidence.status == "failed":
            raise ArtifactActivationError("failed Policy evidence cannot be approved")
        if evidence.status == "review_required":
            if not accept_findings:
                raise ArtifactActivationError(
                    "review_required evidence needs explicit findings acceptance"
                )
            if reason is None or not reason.strip():
                raise ArtifactActivationError(
                    "review_required evidence needs a non-empty reason"
                )
        self.artifact_store.import_review_snapshot(source, evidence)
        return self.activation_store.add(
            candidate=evidence.candidate,
            decision=ActivationDecision.APPROVED,
            decided_by=operator,
            evidence=(f"policy-review:{evidence.review_id}",),
            reason=reason,
            metadata={
                "review_id": evidence.review_id,
                "evidence_digest": evidence.evidence_digest,
                "accepted_findings": accept_findings,
            },
        )

    def deny(
        self,
        evidence: PolicyReviewEvidence,
        *,
        operator: str,
        reason: str | None = None,
    ) -> ActivationRecord:
        return self.activation_store.add(
            candidate=evidence.candidate,
            decision=ActivationDecision.DENIED,
            decided_by=operator,
            evidence=(f"policy-review:{evidence.review_id}",),
            reason=reason,
            metadata={
                "review_id": evidence.review_id,
                "evidence_digest": evidence.evidence_digest,
            },
        )


class PolicySelectionStore:
    """Append-only audit log whose projection selects one digest per Policy."""

    def __init__(self, path: str | Path) -> None:
        expanded = Path(path).expanduser()
        _reject_selection_store_symlink(expanded)
        self.path = expanded.resolve()
        self._records: list[PolicyActivationRecord] = []
        if self.path.exists():
            self._load()

    def records(self) -> tuple[PolicyActivationRecord, ...]:
        return tuple(self._records)

    def active_records(self) -> tuple[PolicyActivationRecord, ...]:
        active: dict[str, PolicyActivationRecord] = {}
        for record in self._records:
            if record.action == "deactivate":
                active.pop(record.policy_name, None)
            else:
                active[record.policy_name] = record
        return tuple(active[name] for name in sorted(active))

    def add(self, record: PolicyActivationRecord) -> PolicyActivationRecord:
        with EvolutionFileLock(self.path.with_name(f"{self.path.name}.lock")):
            if self.path.exists():
                self._load()
            if any(existing.record_id == record.record_id for existing in self._records):
                raise ArtifactActivationError(
                    f"duplicate Policy selection record ID: {record.record_id}"
                )
            self._records.append(record)
            try:
                self._save()
            except Exception:
                self._records.pop()
                raise
        return record

    def _save(self) -> None:
        _reject_selection_store_symlink(self.path)
        payload = {
            "schema_version": POLICY_SELECTION_SCHEMA_VERSION,
            "records": [_selection_to_dict(record) for record in self._records],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ArtifactActivationError(
                f"could not persist Policy selection store: {exc}"
            ) from exc

    def _load(self) -> None:
        _reject_selection_store_symlink(self.path)
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ArtifactActivationError(f"invalid Policy selection store: {exc}") from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "records"}
            or type(raw.get("schema_version")) is not int
            or raw["schema_version"] != POLICY_SELECTION_SCHEMA_VERSION
        ):
            raise ArtifactActivationError("unsupported Policy selection store schema")
        records = raw.get("records")
        if not isinstance(records, list):
            raise ArtifactActivationError("Policy selection records must be an array")
        loaded = [_selection_from_dict(item) for item in records]
        record_ids = [record.record_id for record in loaded]
        if len(record_ids) != len(set(record_ids)):
            raise ArtifactActivationError("duplicate Policy selection record ID")
        self._records = loaded


class PolicyActivationService:
    def __init__(
        self,
        activation_store: ActivationStore,
        artifact_store: PolicyArtifactStore,
        selection_store: PolicySelectionStore,
    ) -> None:
        self.activation_store = activation_store
        self.artifact_store = artifact_store
        self.selection_store = selection_store

    def activate(
        self,
        approval_record_id: str,
        *,
        operator: str,
        replacement: PolicyReplacement | None = None,
    ) -> PolicyActivationRecord:
        approval = self._require_approval(approval_record_id)
        previous = next(
            (
                record
                for record in self.selection_store.active_records()
                if record.policy_name == approval.candidate.name
            ),
            None,
        )
        return self.selection_store.add(
            PolicyActivationRecord(
                policy_name=approval.candidate.name,
                action="activate",
                operator=_operator(operator),
                approval_record_id=approval.record_id,
                candidate_digest=approval.candidate.digest,
                previous_approval_id=(
                    previous.approval_record_id if previous is not None else None
                ),
                replacement=replacement,
            )
        )

    def deactivate(
        self,
        policy_name: str,
        *,
        operator: str,
        reason: str | None = None,
    ) -> PolicyActivationRecord:
        if not any(
            record.policy_name == policy_name
            for record in self.selection_store.active_records()
        ):
            raise ArtifactActivationError(f"Policy is not active: {policy_name}")
        return self.selection_store.add(
            PolicyActivationRecord(
                policy_name=policy_name,
                action="deactivate",
                operator=_operator(operator),
                reason=reason,
            )
        )

    def rollback(
        self,
        policy_name: str,
        *,
        operator: str,
        to_approval_id: str | None = None,
    ) -> PolicyActivationRecord:
        current = next(
            (
                record
                for record in self.selection_store.active_records()
                if record.policy_name == policy_name
            ),
            None,
        )
        if current is None:
            raise ArtifactActivationError(f"Policy is not active: {policy_name}")
        if to_approval_id is not None:
            target = self._require_approval(to_approval_id)
            if target.candidate.name != policy_name:
                raise ArtifactActivationError("rollback approval belongs to another Policy")
        else:
            target = self._previous_approval(policy_name, current.approval_record_id)
        return self.selection_store.add(
            PolicyActivationRecord(
                policy_name=policy_name,
                action="rollback",
                operator=_operator(operator),
                approval_record_id=target.record_id,
                candidate_digest=target.candidate.digest,
                previous_approval_id=current.approval_record_id,
            )
        )

    def active(self) -> tuple[ActivePolicySelection, ...]:
        resolved: list[ActivePolicySelection] = []
        for selection in self.selection_store.active_records():
            if selection.approval_record_id is None:
                raise ArtifactActivationError("active Policy selection has no approval")
            approval = self._require_approval(selection.approval_record_id)
            if approval.candidate.name != selection.policy_name:
                raise ArtifactActivationError(
                    "active Policy selection does not match its approval"
                )
            if approval.candidate.digest != selection.candidate_digest:
                raise ArtifactActivationError(
                    "active Policy selection digest does not match its approval"
                )
            resolved.append(
                ActivePolicySelection(
                    selection=selection,
                    approval=approval,
                    artifact_path=self.artifact_store.validate(approval.candidate),
                )
            )
        return tuple(resolved)

    def _require_approval(self, record_id: str) -> ActivationRecord:
        record = self.activation_store.get(record_id)
        checked = self.activation_store.check(record.candidate)
        if (
            record.decision is not ActivationDecision.APPROVED
            or not checked.approved
            or checked.record is None
            or checked.record.record_id != record.record_id
        ):
            raise ArtifactActivationError(
                f"Policy activation record is not approved: {record_id}"
            )
        self.artifact_store.validate(record.candidate)
        return record

    def _previous_approval(
        self,
        policy_name: str,
        current_id: str | None,
    ) -> ActivationRecord:
        seen: set[str] = set()
        for record in reversed(self.selection_store.records()):
            approval_id = record.approval_record_id
            if (
                record.policy_name != policy_name
                or approval_id is None
                or approval_id == current_id
                or approval_id in seen
            ):
                continue
            seen.add(approval_id)
            try:
                return self._require_approval(approval_id)
            except ArtifactActivationError:
                continue
        raise ArtifactActivationError(
            f"Policy '{policy_name}' has no previous approved activation"
        )


def _selection_to_dict(record: PolicyActivationRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["created_at"] = record.created_at.isoformat()
    return payload


def _selection_from_dict(raw: object) -> PolicyActivationRecord:
    fields = {
        "record_id",
        "policy_name",
        "action",
        "operator",
        "approval_record_id",
        "candidate_digest",
        "previous_approval_id",
        "replacement",
        "reason",
        "created_at",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ArtifactActivationError("Policy selection record must be an object")
    try:
        created_at = datetime.fromisoformat(_string(raw["created_at"], "created_at"))
        if created_at.tzinfo is None:
            raise ValueError("created_at must include timezone")
        replacement_raw = raw["replacement"]
        if replacement_raw is None:
            replacement = None
        elif isinstance(replacement_raw, dict) and set(replacement_raw) == {
            "policy_name",
            "expected_digest",
        }:
            replacement = PolicyReplacement(
                policy_name=_string(
                    replacement_raw["policy_name"],
                    "replacement policy_name",
                ),
                expected_digest=_string(
                    replacement_raw["expected_digest"],
                    "replacement expected_digest",
                ),
            )
        else:
            raise ValueError("replacement must be null or an exact replacement object")
        action = _string(raw["action"], "action")
        reason = raw["reason"]
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string or null")
        return PolicyActivationRecord(
            record_id=_string(raw["record_id"], "record_id"),
            policy_name=_string(raw["policy_name"], "policy_name"),
            action=cast(PolicyActivationAction, action),
            operator=_string(raw["operator"], "operator"),
            approval_record_id=_optional_string(raw["approval_record_id"]),
            candidate_digest=_optional_string(raw["candidate_digest"]),
            previous_approval_id=_optional_string(raw["previous_approval_id"]),
            replacement=replacement,
            reason=reason,
            created_at=created_at.astimezone(UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactActivationError(f"invalid Policy selection record: {exc}") from exc


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected a string or null")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _operator(value: str) -> str:
    if not value.strip():
        raise ValueError("operator must not be empty")
    return value


def _validate_digest(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("expected a SHA-256 hexadecimal digest")
    digest = value.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("expected a SHA-256 hexadecimal digest")


def _is_hex(value: object, *, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _reject_selection_store_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ArtifactActivationError(
            f"refusing symbolic link Policy selection store: {path}"
        )


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
    "POLICY_SELECTION_SCHEMA_VERSION",
    "ActivePolicySelection",
    "PolicyActivationAction",
    "PolicyActivationRecord",
    "PolicyActivationService",
    "PolicyApprovalService",
    "PolicyArtifactStore",
    "PolicyReplacement",
    "PolicySelectionStore",
]
