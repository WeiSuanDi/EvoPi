"""Workspace trust records for project-owned executable resources."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

WORKSPACE_TRUST_SCHEMA_VERSION = 1


@dataclass(slots=True, frozen=True, kw_only=True)
class WorkspaceTrustRecord:
    workspace: str
    workspace_digest: str
    trusted_by: str
    trusted_at: datetime


class WorkspaceTrustStore:
    """Small versioned store used before project resources are loaded."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._records: dict[str, WorkspaceTrustRecord] = {}
        if self.path.exists():
            self._load()

    def trust(
        self,
        workspace: str | Path,
        *,
        trusted_by: str,
    ) -> WorkspaceTrustRecord:
        if not trusted_by.strip():
            raise ValueError("trusted_by must not be empty")
        normalized = _normalize_workspace(workspace)
        record = WorkspaceTrustRecord(
            workspace=normalized,
            workspace_digest=_workspace_digest(normalized),
            trusted_by=trusted_by,
            trusted_at=datetime.now(UTC),
        )
        self._records[record.workspace_digest] = record
        self._save()
        return record

    def is_trusted(self, workspace: str | Path) -> bool:
        normalized = _normalize_workspace(workspace)
        record = self._records.get(_workspace_digest(normalized))
        return record is not None and record.workspace == normalized

    def records(self) -> tuple[WorkspaceTrustRecord, ...]:
        return tuple(self._records.values())

    def _save(self) -> None:
        payload = {
            "schema_version": WORKSPACE_TRUST_SCHEMA_VERSION,
            "workspaces": [
                {
                    "workspace": record.workspace,
                    "workspace_digest": record.workspace_digest,
                    "trusted_by": record.trusted_by,
                    "trusted_at": record.trusted_at.isoformat(),
                }
                for record in self._records.values()
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != WORKSPACE_TRUST_SCHEMA_VERSION
            or not isinstance(raw.get("workspaces"), list)
        ):
            raise ValueError("invalid workspace trust store")
        for item in raw["workspaces"]:
            if not isinstance(item, dict):
                raise ValueError("invalid workspace trust record")
            record = WorkspaceTrustRecord(
                workspace=str(item["workspace"]),
                workspace_digest=str(item["workspace_digest"]),
                trusted_by=str(item["trusted_by"]),
                trusted_at=datetime.fromisoformat(str(item["trusted_at"])),
            )
            if (
                record.workspace_digest != _workspace_digest(record.workspace)
                or record.trusted_at.tzinfo is None
            ):
                raise ValueError("invalid workspace trust record")
            self._records[record.workspace_digest] = record


def _normalize_workspace(workspace: str | Path) -> str:
    return os.path.normcase(str(Path(workspace).expanduser().resolve()))


def _workspace_digest(workspace: str) -> str:
    return hashlib.sha256(workspace.encode("utf-8")).hexdigest()


__all__ = [
    "WORKSPACE_TRUST_SCHEMA_VERSION",
    "WorkspaceTrustRecord",
    "WorkspaceTrustStore",
]
