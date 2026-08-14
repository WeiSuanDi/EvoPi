"""Workspace trust records for project-owned executable resources."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
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
        expanded = Path(path).expanduser()
        _reject_symlink(expanded)
        self.path = expanded.resolve()
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
        _reject_symlink(self.path)
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
        _reject_symlink(self.path)
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid workspace trust store") from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "workspaces"}
            or type(raw.get("schema_version")) is not int
            or raw["schema_version"] != WORKSPACE_TRUST_SCHEMA_VERSION
            or not isinstance(raw.get("workspaces"), list)
        ):
            raise ValueError("invalid workspace trust store")
        for item in raw["workspaces"]:
            if (
                not isinstance(item, dict)
                or set(item)
                != {
                    "workspace",
                    "workspace_digest",
                    "trusted_by",
                    "trusted_at",
                }
                or any(not isinstance(item[key], str) for key in item)
                or not item["trusted_by"].strip()
            ):
                raise ValueError("invalid workspace trust record")
            try:
                trusted_at = datetime.fromisoformat(item["trusted_at"])
            except ValueError as exc:
                raise ValueError("invalid workspace trust record") from exc
            workspace = item["workspace"]
            digest = item["workspace_digest"]
            if (
                workspace != _normalize_workspace(workspace)
                or digest != _workspace_digest(workspace)
                or trusted_at.utcoffset() != timedelta(0)
                or digest in self._records
            ):
                raise ValueError("invalid workspace trust record")
            record = WorkspaceTrustRecord(
                workspace=workspace,
                workspace_digest=digest,
                trusted_by=item["trusted_by"],
                trusted_at=trusted_at,
            )
            self._records[record.workspace_digest] = record


def _normalize_workspace(workspace: str | Path) -> str:
    return os.path.normcase(str(Path(workspace).expanduser().resolve()))


def _workspace_digest(workspace: str) -> str:
    return hashlib.sha256(workspace.encode("utf-8")).hexdigest()


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symbolic link workspace trust store: {path}")


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
    "WORKSPACE_TRUST_SCHEMA_VERSION",
    "WorkspaceTrustRecord",
    "WorkspaceTrustStore",
]
