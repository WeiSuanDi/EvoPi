"""Non-executing inspection and immutable snapshots for Policy candidates."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from evopi.evolution.activation import ArtifactCandidate
from evopi.harness.hooks import HOOKS
from evopi.policy.types import HookName, RiskLevel

POLICY_MANIFEST_SCHEMA_VERSION = 1
_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})


class PolicyCandidateError(RuntimeError):
    """Raised when a Policy candidate cannot be inspected safely."""


class PolicyCandidateStatus(str, Enum):
    DISCOVERED = "discovered"
    CANDIDATE = "candidate"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    ACTIVE = "active"
    DENIED = "denied"
    STALE = "stale"
    FAILED = "failed"


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyManifest:
    name: str
    version: str
    description: str
    entrypoint: str
    hooks: tuple[HookName, ...]
    priority: int
    source: str
    risk_level: RiskLevel
    metadata: dict[str, Any]
    dry_run_entrypoint: str | None = None
    schema_version: int = POLICY_MANIFEST_SCHEMA_VERSION


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyCandidate:
    path: Path
    manifest: PolicyManifest
    artifact: ArtifactCandidate


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyCandidateInspection:
    candidate: PolicyCandidate
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors


class PolicyCandidateSnapshotStore:
    """Content-addressed snapshots frozen before candidate execution."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def path_for(self, digest: str) -> Path:
        return self.root / "snapshots" / digest

    def freeze(self, candidate: PolicyCandidate) -> Path:
        current = inspect_policy_candidate(candidate.path).candidate
        if current.artifact.digest != candidate.artifact.digest:
            raise PolicyCandidateError(
                "Policy candidate changed after inspection; inspect it again"
            )
        target = self.path_for(candidate.artifact.digest)
        if target.exists():
            snapshot = inspect_policy_candidate(target).candidate
            if snapshot.artifact.digest != candidate.artifact.digest:
                raise PolicyCandidateError(
                    "Policy candidate snapshot digest does not match its path"
                )
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            shutil.copytree(candidate.path, temporary)
            snapshot = inspect_policy_candidate(temporary).candidate
            if snapshot.artifact.digest != candidate.artifact.digest:
                raise PolicyCandidateError(
                    "copied Policy candidate failed digest verification"
                )
            os.replace(temporary, target)
        except (OSError, shutil.Error, PolicyCandidateError):
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return target


def inspect_policy_candidate(path: str | Path) -> PolicyCandidateInspection:
    """Inspect one directory without importing or executing candidate Python."""

    candidate_path = Path(path).expanduser().resolve()
    if not candidate_path.is_dir():
        raise PolicyCandidateError(f"Policy candidate directory does not exist: {candidate_path}")
    _reject_links(candidate_path)
    manifest = _read_manifest(candidate_path)
    _resolve_entrypoint(candidate_path, manifest.entrypoint)
    if manifest.dry_run_entrypoint is not None:
        _resolve_entrypoint(candidate_path, manifest.dry_run_entrypoint)

    errors: list[str] = []
    for source in sorted(candidate_path.rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            errors.append(f"{source.name} is not valid UTF-8 Python: {exc}")

    digest = policy_candidate_digest(candidate_path)
    artifact = ArtifactCandidate(
        kind="policy",
        name=manifest.name,
        version=manifest.version,
        source=str(candidate_path),
        risk_level=manifest.risk_level,
        digest=digest,
        metadata={
            "description": manifest.description,
            "entrypoint": manifest.entrypoint,
            "hooks": list(manifest.hooks),
            "priority": manifest.priority,
            "source": manifest.source,
            "dry_run_entrypoint": manifest.dry_run_entrypoint,
            "manifest_metadata": dict(manifest.metadata),
        },
    )
    return PolicyCandidateInspection(
        candidate=PolicyCandidate(
            path=candidate_path,
            manifest=manifest,
            artifact=artifact,
        ),
        errors=tuple(errors),
    )


def policy_candidate_digest(path: str | Path) -> str:
    root = Path(path).expanduser().resolve()
    _reject_links(root)
    digest = hashlib.sha256()
    files = sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix != ".pyc"
    )
    for item in files:
        relative = item.relative_to(root).as_posix().encode("utf-8")
        content = item.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def resolve_policy_entrypoint(root: str | Path, spec: str) -> tuple[Path, str]:
    return _resolve_entrypoint(Path(root).expanduser().resolve(), spec)


def _read_manifest(path: Path) -> PolicyManifest:
    manifest_path = path / "evopi-policy.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyCandidateError(f"invalid Policy manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyCandidateError("Policy manifest must be an object")
    if raw.get("schema_version") != POLICY_MANIFEST_SCHEMA_VERSION:
        raise PolicyCandidateError("unsupported Policy manifest schema")
    required = (
        "name",
        "version",
        "description",
        "entrypoint",
        "hooks",
        "priority",
        "source",
        "risk_level",
    )
    missing = [name for name in required if name not in raw]
    if missing:
        raise PolicyCandidateError(
            f"missing Policy manifest fields: {', '.join(missing)}"
        )
    for name in ("name", "version", "description", "entrypoint", "source"):
        if not isinstance(raw[name], str) or not raw[name].strip():
            raise PolicyCandidateError(f"Policy manifest {name} must be a non-empty string")
    raw_hooks = raw["hooks"]
    if (
        not isinstance(raw_hooks, list)
        or not raw_hooks
        or any(not isinstance(item, str) or item not in HOOKS for item in raw_hooks)
    ):
        raise PolicyCandidateError("Policy manifest hooks must contain supported Hook names")
    priority = raw["priority"]
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise PolicyCandidateError("Policy manifest priority must be an integer")
    risk_level = raw["risk_level"]
    if risk_level not in _RISK_LEVELS:
        raise PolicyCandidateError("Policy manifest risk_level is invalid")
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise PolicyCandidateError("Policy manifest metadata must be an object")
    _require_json_safe(metadata, "Policy manifest metadata")
    dry_run = raw.get("dry_run_entrypoint")
    if dry_run is not None and (not isinstance(dry_run, str) or not dry_run.strip()):
        raise PolicyCandidateError(
            "Policy manifest dry_run_entrypoint must be a non-empty string or null"
        )
    return PolicyManifest(
        name=raw["name"],
        version=raw["version"],
        description=raw["description"],
        entrypoint=raw["entrypoint"],
        hooks=tuple(cast(HookName, item) for item in raw_hooks),
        priority=priority,
        source=raw["source"],
        risk_level=cast(RiskLevel, risk_level),
        metadata=dict(metadata),
        dry_run_entrypoint=dry_run,
    )


def _resolve_entrypoint(root: Path, spec: str) -> tuple[Path, str]:
    relative, separator, attribute = spec.partition(":")
    if not separator or not relative or not attribute:
        raise PolicyCandidateError("Policy entrypoint must use relative.py:ATTRIBUTE syntax")
    entry = (root / relative).resolve()
    try:
        entry.relative_to(root)
    except ValueError as exc:
        raise PolicyCandidateError("Policy entrypoint escapes candidate directory") from exc
    if entry.suffix != ".py" or not entry.is_file():
        raise PolicyCandidateError(f"Policy entrypoint does not exist: {entry}")
    if any(not part for part in attribute.split(".")):
        raise PolicyCandidateError("Policy entrypoint attribute is invalid")
    return entry, attribute


def _reject_links(root: Path) -> None:
    if root.is_symlink():
        raise PolicyCandidateError("Policy candidate directory must not be a symbolic link")
    for item in root.rglob("*"):
        if item.is_symlink():
            raise PolicyCandidateError(
                f"Policy candidate contains a symbolic link: {item.relative_to(root)}"
            )


def _require_json_safe(value: Any, label: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PolicyCandidateError(f"{label} must be strictly JSON-safe") from exc


__all__ = [
    "POLICY_MANIFEST_SCHEMA_VERSION",
    "PolicyCandidate",
    "PolicyCandidateError",
    "PolicyCandidateInspection",
    "PolicyCandidateSnapshotStore",
    "PolicyCandidateStatus",
    "PolicyManifest",
    "inspect_policy_candidate",
    "policy_candidate_digest",
    "resolve_policy_entrypoint",
]
