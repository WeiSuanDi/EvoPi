"""Mandatory, redacted and hash-chained Remote security audit log."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from evopi.configuration import harden_credential_permissions
from evopi.evolution.file_lock import EvolutionFileLock, EvolutionStoreLockError

from .errors import RemoteAuditError

_SENSITIVE_KEYS = frozenset(
    {"prompt", "content", "arguments", "api_key", "token", "signature", "private_key"}
)
_GENESIS = "0" * 64


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteAuditEntry:
    entry_id: str
    created_at: datetime
    action: str
    outcome: str
    device_id: str | None
    client_ip: str | None
    details: Mapping[str, Any]
    previous_hash: str
    entry_hash: str
    schema_version: int = 1


class RemoteAuditLog:
    def __init__(self, root: Path, *, max_segment_bytes: int = 50 * 1024 * 1024) -> None:
        if max_segment_bytes <= 0:
            raise ValueError("max_segment_bytes must be positive")
        self.root = root.resolve()
        self.max_segment_bytes = max_segment_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()
        self._last_hash = self._find_last_hash()
        self.prune_expired_client_ips()

    @property
    def current_path(self) -> Path:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        candidates = sorted(self.root.glob(f"remote-audit-{day}-*.jsonl"))
        if not candidates:
            return self.root / f"remote-audit-{day}-0001.jsonl"
        current = candidates[-1]
        if current.stat().st_size >= self.max_segment_bytes:
            index = int(current.stem.rsplit("-", 1)[1]) + 1
            return self.root / f"remote-audit-{day}-{index:04d}.jsonl"
        return current

    def append(
        self,
        *,
        action: str,
        outcome: str,
        device_id: str | None = None,
        client_ip: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> RemoteAuditEntry:
        safe_details = dict(details or {})
        _reject_sensitive(safe_details)
        if not action or not outcome:
            raise RemoteAuditError("audit action and outcome must be non-empty")
        now = datetime.now(UTC)
        try:
            with self._thread_lock:
                base: dict[str, Any] = {
                    "schema_version": 1,
                    "entry_id": str(uuid4()),
                    "created_at": now.isoformat(),
                    "action": action,
                    "outcome": outcome,
                    "device_id": device_id,
                    "client_ip_sha256": (
                        hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
                        if client_ip is not None
                        else None
                    ),
                    "details": safe_details,
                    "previous_hash": self._last_hash,
                }
                digest = _digest(base)
                payload = {**base, "entry_hash": digest}
                path = self.current_path
                with EvolutionFileLock(self.root / "audit.lock"):
                    if client_ip is not None:
                        self._append_client_ip(
                            entry_id=base["entry_id"], created_at=now, client_ip=client_ip
                        )
                    created = not path.exists()
                    descriptor = os.open(
                        path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
                    )
                    with os.fdopen(
                        descriptor, "a", encoding="utf-8", newline="\n"
                    ) as handle:
                        handle.write(_canonical(payload) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    if created:
                        harden_credential_permissions(path)
                self._last_hash = digest
        except (OSError, subprocess.SubprocessError, EvolutionStoreLockError) as exc:
            raise RemoteAuditError("Remote audit write failed") from exc
        return RemoteAuditEntry(
            entry_id=base["entry_id"],
            created_at=now,
            action=action,
            outcome=outcome,
            device_id=device_id,
            client_ip=client_ip,
            details=safe_details,
            previous_hash=base["previous_hash"],
            entry_hash=digest,
        )

    def prune_expired_client_ips(
        self,
        *,
        now: datetime | None = None,
        retention_days: int = 30,
    ) -> tuple[Path, ...]:
        """Delete raw-IP sidecars older than the configured retention window."""

        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = current.astimezone(UTC).date() - timedelta(days=retention_days)
        removed: list[Path] = []
        try:
            with self._thread_lock, EvolutionFileLock(self.root / "audit.lock"):
                for path in sorted(self.root.glob("remote-client-ip-????-??-??.jsonl")):
                    try:
                        raw_day = path.stem.removeprefix("remote-client-ip-")
                        day = datetime.strptime(raw_day, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if day < cutoff:
                        path.unlink()
                        removed.append(path)
        except (OSError, EvolutionStoreLockError) as exc:
            raise RemoteAuditError("Remote audit IP retention cleanup failed") from exc
        return tuple(removed)

    def _append_client_ip(
        self, *, entry_id: str, created_at: datetime, client_ip: str
    ) -> None:
        path = self.root / f"remote-client-ip-{created_at:%Y-%m-%d}.jsonl"
        payload = {
            "schema_version": 1,
            "entry_id": entry_id,
            "created_at": created_at.isoformat(),
            "client_ip": client_ip,
        }
        created = not path.exists()
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if created:
                harden_credential_permissions(path)
            else:
                path.chmod(0o600)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RemoteAuditError("unable to protect Remote audit IP sidecar") from exc

    def _find_last_hash(self) -> str:
        paths = sorted(self.root.glob("remote-audit-*.jsonl"))
        if not paths:
            return _GENESIS
        previous = _GENESIS
        for path in paths:
            previous = _verify_path(path, expected_previous=previous)[1]
        return previous


def verify_remote_audit_chain(path: Path) -> int:
    return _verify_path(path, expected_previous=_GENESIS)[0]


def _verify_path(path: Path, *, expected_previous: str) -> tuple[int, str]:
    count = 0
    previous = expected_previous
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RemoteAuditError("Remote audit read failed") from exc
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RemoteAuditError(f"invalid audit JSON at line {line_number}") from exc
        if not isinstance(value, dict) or value.get("previous_hash") != previous:
            raise RemoteAuditError(f"audit chain mismatch at line {line_number}")
        actual = value.get("entry_hash")
        base = dict(value)
        base.pop("entry_hash", None)
        if not isinstance(actual, str) or actual != _digest(base):
            raise RemoteAuditError(f"audit digest mismatch at line {line_number}")
        previous = actual
        count += 1
    return count, previous


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _canonical(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise RemoteAuditError("audit value is not JSON-safe") from exc


def _reject_sensitive(value: Mapping[str, Any]) -> None:
    for key, item in value.items():
        if key.lower() in _SENSITIVE_KEYS:
            raise RemoteAuditError("sensitive audit detail key is forbidden")
        if isinstance(item, dict):
            _reject_sensitive(item)


__all__ = ["RemoteAuditEntry", "RemoteAuditLog", "verify_remote_audit_chain"]
