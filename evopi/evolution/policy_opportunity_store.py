"""Immutable persistence for Policy Opportunity reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from evopi.evolution.file_lock import EvolutionFileLock
from evopi.evolution.policy_discovery_protocol import (
    PolicyDiscoveryError,
    PolicyDiscoveryReport,
    policy_discovery_report_from_dict,
)


class PolicyOpportunityStore:
    """Atomically persist digest-bound Policy Discovery reports."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    @property
    def lock_path(self) -> Path:
        return self.root / ".store.lock"

    def report_path(self, report_id: str) -> Path:
        normalized = report_id.lower()
        if len(normalized) != 32 or any(
            char not in "0123456789abcdef" for char in normalized
        ):
            raise PolicyDiscoveryError(
                "report_id must be a 32-character hexadecimal string"
            )
        return self.root / "reports" / f"{normalized}.json"

    def save(self, report: PolicyDiscoveryReport) -> PolicyDiscoveryReport:
        payload = report.to_dict()
        stored = policy_discovery_report_from_dict(payload)
        path = self.report_path(stored.report_id)
        with EvolutionFileLock(self.lock_path):
            if path.exists():
                loaded = self.load(stored.report_id)
                if loaded != stored:
                    raise PolicyDiscoveryError(
                        f"Policy Discovery report already exists with different content: "
                        f"{stored.report_id}"
                    )
                return loaded
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            except OSError as exc:
                temporary.unlink(missing_ok=True)
                raise PolicyDiscoveryError(
                    f"could not persist Policy Discovery report: {exc}"
                ) from exc
        return stored

    def load(self, report_id: str) -> PolicyDiscoveryReport:
        path = self.report_path(report_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolicyDiscoveryError(
                f"invalid Policy Discovery report: {exc}",
                path=path,
            ) from exc
        return policy_discovery_report_from_dict(payload)


__all__ = ["PolicyOpportunityStore"]
