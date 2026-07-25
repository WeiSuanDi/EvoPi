"""Approval records and the activation gate for Policy governance."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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

    def matches(self, policy_name: str, policy_version: str) -> bool:
        return self.policy_name == policy_name and self.policy_version == policy_version


class ApprovalLoaded:
    """Immutable result returned by ApprovalStore.check()."""

    def __init__(
        self,
        record: ApprovalRecord | None,
        mode: ApprovalMode,
    ) -> None:
        self._record = record
        self._mode = mode

    @property
    def approved(self) -> bool:
        return self._record is not None and self._record.decision == "approved"

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
                }
            )
        payload = {"approvals": items}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


__all__ = [
    "ApprovalLoaded",
    "ApprovalMode",
    "ApprovalRecord",
    "ApprovalRequiredError",
    "ApprovalStore",
]
