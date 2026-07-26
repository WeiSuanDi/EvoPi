"""Approval records and the activation gate for Policy governance."""

from __future__ import annotations

import json
import logging
import hashlib
import inspect
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from evopi.policy.types import Policy

_logger = logging.getLogger(__name__)

ApprovalMode = Literal["strict", "warn", "off"]
ApprovalDecision = Literal["approved", "denied"]


@dataclass(slots=True, frozen=True, kw_only=True)
class ApprovalRecord:
    """A single human approval decision for a specific Policy version."""

    policy_name: str
    policy_version: str
    approved_by: str
    approved_at: datetime
    evidence: list[str] = field(default_factory=list)
    decision: ApprovalDecision = "approved"
    reason: str | None = None
    artifact_digest: str | None = None

    def matches(
        self,
        policy_name: str,
        policy_version: str,
        artifact_digest: str | None = None,
    ) -> bool:
        identity_matches = (
            self.policy_name == policy_name
            and self.policy_version == policy_version
        )
        if artifact_digest is None:
            return identity_matches
        return identity_matches and self.artifact_digest == artifact_digest


class ApprovalLoaded:
    """Immutable result returned by ApprovalStore.check()."""

    def __init__(
        self,
        record: ApprovalRecord | None,
        mode: ApprovalMode,
        *,
        digest_required: bool = False,
        digest_mismatch: bool = False,
    ) -> None:
        self._record = record
        self._mode = mode
        self._digest_required = digest_required
        self._digest_mismatch = digest_mismatch

    @property
    def approved(self) -> bool:
        if self._record is None or self._record.decision != "approved":
            return False
        if self._digest_mismatch:
            return False
        if (
            self._digest_required
            and self._mode == "strict"
            and self._record.artifact_digest is None
        ):
            return False
        return True

    @property
    def record(self) -> ApprovalRecord | None:
        return self._record

    @property
    def mode(self) -> ApprovalMode:
        return self._mode

    def raise_if_required(self, policy_name: str, policy_version: str) -> None:
        """Raise if strict mode and the policy is not approved."""
        if self._mode == "off":
            return
        if self._mode == "strict" and not self.approved:
            if self._record is not None and self._record.decision == "denied":
                raise ApprovalRequiredError(
                    f"Policy '{policy_name}@{policy_version}' was explicitly denied"
                )
            raise ApprovalRequiredError(
                f"Policy '{policy_name}@{policy_version}' has not been approved"
            )
        if self._mode == "warn" and not self.approved:
            if self._record is not None and self._record.decision == "denied":
                _logger.warning(
                    "Policy '%s@%s' was denied but is loaded (warn mode)",
                    policy_name,
                    policy_version,
                )
            else:
                _logger.warning(
                    "Policy '%s@%s' has not been approved (warn mode)",
                    policy_name,
                    policy_version,
                )
        if (
            self._mode == "warn"
            and self._digest_required
            and self._record is not None
            and self._record.artifact_digest is None
        ):
            _logger.warning(
                "Policy '%s@%s' uses a legacy approval without a digest; "
                "upgrade the record before strict activation",
                policy_name,
                policy_version,
            )


class ApprovalRequiredError(RuntimeError):
    """Raised when an unapproved policy is loaded in strict mode."""


class ApprovalStore:
    """Persistent store of ApprovalRecords backed by a JSON file."""

    def __init__(self, path: str | Path | None, *, mode: ApprovalMode = "warn") -> None:
        self._path = Path(path) if path is not None else None
        self._mode: ApprovalMode = mode
        self._records: list[ApprovalRecord] = []
        self._by_key: dict[str, ApprovalRecord] = {}
        if self._path is not None:
            self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def mode(self) -> ApprovalMode:
        return self._mode

    @property
    def path(self) -> Path | None:
        return self._path

    def records(self) -> list[ApprovalRecord]:
        return list(self._records)

    def add(
        self,
        *,
        policy_name: str,
        policy_version: str,
        approved_by: str,
        evidence: list[str] | None = None,
        decision: ApprovalDecision = "approved",
        reason: str | None = None,
        artifact_digest: str | None = None,
        approved_at: datetime | None = None,
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            policy_name=policy_name,
            policy_version=policy_version,
            approved_by=approved_by,
            approved_at=approved_at or datetime.now(UTC),
            evidence=evidence or [],
            decision=decision,
            reason=reason,
            artifact_digest=artifact_digest,
        )
        key = self._key(policy_name, policy_version)
        if key in self._by_key:
            raise ApprovalRequiredError(
                f"Approval for '{policy_name}@{policy_version}' already exists; "
                f"remove it first or use explicit replacement."
            )
        self._records.append(record)
        self._by_key[key] = record
        if self._path is not None:
            self._save()
        return record

    def check(self, policy_name: str, policy_version: str) -> ApprovalLoaded:
        """Check approval status for a policy. Never raises — the caller
        calls .raise_if_required() when they are ready to act."""
        key = self._key(policy_name, policy_version)
        record = self._by_key.get(key)
        return ApprovalLoaded(record, self._mode)

    def check_policy(self, policy: Policy) -> ApprovalLoaded:
        digest = policy_digest(policy)
        record = self._by_key.get(self._key(policy.name, policy.version))
        mismatch = (
            record is not None
            and record.artifact_digest is not None
            and record.artifact_digest != digest
        )
        return ApprovalLoaded(
            record,
            self._mode,
            digest_required=True,
            digest_mismatch=mismatch,
        )

    def add_policy(
        self,
        policy: Policy,
        *,
        approved_by: str,
        evidence: list[str] | None = None,
        decision: ApprovalDecision = "approved",
        reason: str | None = None,
    ) -> ApprovalRecord:
        return self.add(
            policy_name=policy.name,
            policy_version=policy.version,
            approved_by=approved_by,
            evidence=evidence,
            decision=decision,
            reason=reason,
            artifact_digest=policy_digest(policy),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _key(policy_name: str, policy_version: str) -> str:
        return f"{policy_name}@{policy_version}"

    def _load(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            _logger.debug("Approval file not found, starting empty: %s", self._path)
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ApprovalRequiredError(
                f"Approval file is not valid JSON: {self._path}\n{exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ApprovalRequiredError(
                f"Approval file must contain a JSON object: {self._path}"
            )
        items: list[dict[str, Any]] = raw.get("approvals", [])
        if not isinstance(items, list):
            raise ApprovalRequiredError(
                f"'approvals' in approval file must be a list: {self._path}"
            )
        self._records = []
        self._by_key = {}
        for item in items:
            record = self._parse_record(item)
            key = self._key(record.policy_name, record.policy_version)
            if key in self._by_key:
                raise ApprovalRequiredError(
                    f"Duplicate approval for '{record.policy_name}@{record.policy_version}' "
                    f"in {self._path}"
                )
            self._records.append(record)
            self._by_key[key] = record

    def _parse_record(self, item: dict[str, Any]) -> ApprovalRecord:
        try:
            approved_at = item["approved_at"]
            if isinstance(approved_at, str):
                approved_at = datetime.fromisoformat(approved_at)
            return ApprovalRecord(
                policy_name=item["policy_name"],
                policy_version=item["policy_version"],
                approved_by=item["approved_by"],
                approved_at=approved_at,
                evidence=item.get("evidence", []),
                decision=item.get("decision", "approved"),
                reason=item.get("reason"),
                artifact_digest=item.get("artifact_digest"),
            )
        except KeyError as exc:
            raise ApprovalRequiredError(
                f"Missing required field {exc} in approval record in {self._path}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ApprovalRequiredError(
                f"Invalid approval record in {self._path}: {exc}"
            ) from exc

    def _save(self) -> None:
        assert self._path is not None
        items: list[dict[str, Any]] = []
        for record in self._records:
            items.append(
                {
                    "policy_name": record.policy_name,
                    "policy_version": record.policy_version,
                    "approved_by": record.approved_by,
                    "approved_at": record.approved_at.isoformat(),
                    "evidence": record.evidence,
                    "decision": record.decision,
                    "reason": record.reason,
                    "artifact_digest": record.artifact_digest,
                }
            )
        payload = {"schema_version": 2, "approvals": items}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(
            f".{self._path.name}.{uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise


def policy_digest(policy: Policy) -> str:
    """Bind approval to executable class source plus declared Policy contract."""

    try:
        source = inspect.getsource(type(policy))
    except (OSError, TypeError):
        source = f"{type(policy).__module__}.{type(policy).__qualname__}"
    payload = {
        "class": f"{type(policy).__module__}.{type(policy).__qualname__}",
        "source": source,
        "name": policy.name,
        "version": policy.version,
        "description": policy.description,
        "hooks": sorted(policy.hooks),
        "priority": policy.priority,
        "source_kind": policy.source,
        "risk_level": policy.risk_level,
        "metadata": policy.metadata,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "ApprovalLoaded",
    "ApprovalMode",
    "ApprovalRecord",
    "ApprovalRequiredError",
    "ApprovalStore",
    "policy_digest",
]
