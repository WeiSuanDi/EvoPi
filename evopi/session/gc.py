"""Safe planning and application of derived Checkpoint garbage collection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import re
from typing import TYPE_CHECKING, Literal, TypeAlias

from evopi.session.checkpoint import SessionCheckpoint, load_checkpoint
from evopi.session.errors import SessionError, SessionFormatError
from evopi.session.tree import CheckpointEntry

if TYPE_CHECKING:
    from evopi.session.session import SessionManager

CheckpointGCCategory: TypeAlias = Literal[
    "valid",
    "corrupt",
    "orphan",
    "temporary",
    "missing",
]

_CHECKPOINT_NAME = re.compile(r"^[0-9a-fA-F]{32}\.json$")
_TEMPORARY_NAME = re.compile(r"^\..+\.tmp$")


class CheckpointGCError(SessionError):
    """Raised when a GC plan cannot be built or safely applied."""


@dataclass(slots=True, frozen=True, kw_only=True)
class CheckpointGCSettings:
    keep_per_leaf: int = 3
    protect_days: int = 7

    def __post_init__(self) -> None:
        if self.keep_per_leaf < 1:
            raise ValueError("Checkpoint keep_per_leaf must be at least 1")
        if self.protect_days < 0:
            raise ValueError("Checkpoint protect_days cannot be negative")


DEFAULT_CHECKPOINT_GC_SETTINGS = CheckpointGCSettings()


@dataclass(slots=True, frozen=True, kw_only=True)
class CheckpointGCItem:
    relative_path: str
    category: CheckpointGCCategory
    size_bytes: int
    sha256: str
    modified_at: datetime | None
    referenced: bool
    protected: bool
    eligible: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "category": self.category,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "modified_at": (
                self.modified_at.isoformat()
                if self.modified_at is not None
                else None
            ),
            "referenced": self.referenced,
            "protected": self.protected,
            "eligible": self.eligible,
            "reason": self.reason,
        }


@dataclass(slots=True, frozen=True, kw_only=True)
class CheckpointGCPlan:
    session_id: str
    session_path: str
    log_sha256: str
    settings: CheckpointGCSettings
    items: tuple[CheckpointGCItem, ...]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = 1

    @property
    def candidates(self) -> tuple[CheckpointGCItem, ...]:
        return tuple(item for item in self.items if item.eligible)

    @property
    def protected(self) -> tuple[CheckpointGCItem, ...]:
        return tuple(item for item in self.items if item.protected)

    @property
    def missing(self) -> tuple[CheckpointGCItem, ...]:
        return tuple(item for item in self.items if item.category == "missing")

    @property
    def kept(self) -> tuple[CheckpointGCItem, ...]:
        return tuple(
            item
            for item in self.items
            if not item.eligible and item.category != "missing"
        )

    @property
    def estimated_bytes(self) -> int:
        return sum(item.size_bytes for item in self.candidates)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "session_path": self.session_path,
            "created_at": self.created_at.isoformat(),
            "log_sha256": self.log_sha256,
            "settings": {
                "keep_per_leaf": self.settings.keep_per_leaf,
                "protect_days": self.settings.protect_days,
            },
            "item_count": len(self.items),
            "kept_count": len(self.kept),
            "protected_count": len(self.protected),
            "missing_count": len(self.missing),
            "candidate_count": len(self.candidates),
            "estimated_bytes": self.estimated_bytes,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(slots=True, frozen=True, kw_only=True)
class CheckpointGCFailure:
    relative_path: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {
            "relative_path": self.relative_path,
            "error": self.error,
        }


@dataclass(slots=True, frozen=True, kw_only=True)
class CheckpointGCReport:
    session_id: str
    session_path: str
    plan_created_at: datetime
    applied: bool
    kept_count: int
    protected_count: int
    missing_count: int
    candidate_count: int
    estimated_bytes: int
    deleted_count: int = 0
    reclaimed_bytes: int = 0
    errors: tuple[CheckpointGCFailure, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = 1

    @property
    def passed(self) -> bool:
        return not self.errors

    @classmethod
    def preview(cls, plan: CheckpointGCPlan) -> "CheckpointGCReport":
        return cls(
            session_id=plan.session_id,
            session_path=plan.session_path,
            plan_created_at=plan.created_at,
            applied=False,
            kept_count=len(plan.kept),
            protected_count=len(plan.protected),
            missing_count=len(plan.missing),
            candidate_count=len(plan.candidates),
            estimated_bytes=plan.estimated_bytes,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "session_path": self.session_path,
            "plan_created_at": self.plan_created_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "applied": self.applied,
            "passed": self.passed,
            "kept_count": self.kept_count,
            "protected_count": self.protected_count,
            "missing_count": self.missing_count,
            "candidate_count": self.candidate_count,
            "estimated_bytes": self.estimated_bytes,
            "deleted_count": self.deleted_count,
            "reclaimed_bytes": self.reclaimed_bytes,
            "errors": [error.to_dict() for error in self.errors],
        }


def plan_checkpoint_gc(
    manager: "SessionManager",
    settings: CheckpointGCSettings = DEFAULT_CHECKPOINT_GC_SETTINGS,
    *,
    now: datetime | None = None,
) -> CheckpointGCPlan:
    """Build a deterministic, read-only GC plan for one persistent Session."""

    session_path, session_directory, checkpoint_directory = _paths(manager)
    try:
        log_payload = session_path.read_bytes()
    except OSError as exc:
        raise CheckpointGCError(f"Session Log could not be read: {exc}") from exc
    created_at = now or datetime.now(UTC)
    if created_at.tzinfo is None:
        raise ValueError("Checkpoint GC now must be timezone-aware")
    protected_after = created_at - timedelta(days=settings.protect_days)

    referenced: dict[str, CheckpointEntry] = {}
    for entry in manager.entries:
        if not isinstance(entry, CheckpointEntry) or not entry.path:
            continue
        relative, _path = _checkpoint_path(
            session_directory,
            checkpoint_directory,
            entry.path,
        )
        if relative in referenced:
            raise CheckpointGCError(
                f"Session Log references Checkpoint path '{relative}' more than once"
            )
        referenced[relative] = entry

    file_records: dict[str, tuple[Path, int, str, datetime]] = {}
    if checkpoint_directory.exists():
        try:
            checkpoint_files = tuple(checkpoint_directory.iterdir())
        except OSError as exc:
            raise CheckpointGCError(
                f"Checkpoint directory could not be inspected: {exc}"
            ) from exc
        for path in checkpoint_files:
            if path.is_symlink():
                raise CheckpointGCError(
                    f"Checkpoint directory contains a symbolic link: {path.name}"
                )
            if not path.is_file():
                continue
            if not (
                _CHECKPOINT_NAME.fullmatch(path.name)
                or _TEMPORARY_NAME.fullmatch(path.name)
                or f"checkpoints/{path.name}" in referenced
            ):
                continue
            relative, safe_path = _checkpoint_path(
                session_directory,
                checkpoint_directory,
                f"checkpoints/{path.name}",
            )
            try:
                payload = safe_path.read_bytes()
                stat = safe_path.stat()
            except OSError as exc:
                raise CheckpointGCError(
                    f"Checkpoint file '{relative}' could not be inspected: {exc}"
                ) from exc
            file_records[relative] = (
                safe_path,
                stat.st_size,
                hashlib.sha256(payload).hexdigest(),
                datetime.fromtimestamp(stat.st_mtime, UTC),
            )

    valid_paths: set[str] = set()
    corrupt_paths: set[str] = set()
    for relative, entry in referenced.items():
        record = file_records.get(relative)
        if record is None:
            continue
        path = record[0]
        try:
            checkpoint = load_checkpoint(path, expected_sha256=entry.sha256)
            _validate_checkpoint_projection(manager, entry, checkpoint)
        except SessionFormatError:
            corrupt_paths.add(relative)
        else:
            valid_paths.add(relative)

    retained_paths: set[str] = set()
    for leaf_id in manager.leaves():
        valid_on_path: list[str] = []
        for entry in manager._path_to_entry(leaf_id):
            if not isinstance(entry, CheckpointEntry) or not entry.path:
                continue
            relative, _ = _checkpoint_path(
                session_directory,
                checkpoint_directory,
                entry.path,
            )
            if relative in valid_paths:
                valid_on_path.append(relative)
        retained_paths.update(valid_on_path[-settings.keep_per_leaf :])

    items: list[CheckpointGCItem] = []
    for relative, (_path, size, digest, modified_at) in file_records.items():
        referenced_entry = referenced.get(relative)
        if relative in corrupt_paths:
            category: CheckpointGCCategory = "corrupt"
        elif referenced_entry is not None:
            category = "valid"
        elif _TEMPORARY_NAME.fullmatch(Path(relative).name):
            category = "temporary"
        else:
            category = "orphan"
        if modified_at >= protected_after:
            protected = True
            eligible = False
            reason = "protected_by_age"
        elif relative in retained_paths:
            protected = False
            eligible = False
            reason = "retained_per_leaf"
        else:
            protected = False
            eligible = True
            reason = {
                "valid": "redundant_checkpoint",
                "corrupt": "corrupt_checkpoint",
                "orphan": "orphan_checkpoint",
                "temporary": "stale_temporary",
            }[category]
        items.append(
            CheckpointGCItem(
                relative_path=relative,
                category=category,
                size_bytes=size,
                sha256=digest,
                modified_at=modified_at,
                referenced=referenced_entry is not None,
                protected=protected,
                eligible=eligible,
                reason=reason,
            )
        )

    for relative in referenced.keys() - file_records.keys():
        items.append(
            CheckpointGCItem(
                relative_path=relative,
                category="missing",
                size_bytes=0,
                sha256="",
                modified_at=None,
                referenced=True,
                protected=False,
                eligible=False,
                reason="referenced_file_missing",
            )
        )
    return CheckpointGCPlan(
        session_id=manager.session_id,
        session_path=str(session_path),
        log_sha256=hashlib.sha256(log_payload).hexdigest(),
        settings=settings,
        items=tuple(sorted(items, key=lambda item: item.relative_path)),
        created_at=created_at,
    )


def apply_checkpoint_gc(
    manager: "SessionManager",
    plan: CheckpointGCPlan,
) -> CheckpointGCReport:
    """Validate an immutable plan, then delete only its eligible snapshots."""

    session_path, session_directory, checkpoint_directory = _paths(manager)
    if plan.schema_version != 1:
        raise CheckpointGCError(
            f"Unsupported Checkpoint GC plan schema {plan.schema_version}"
        )
    if plan.session_id != manager.session_id:
        raise CheckpointGCError("Checkpoint GC plan belongs to another Session")
    if Path(plan.session_path).resolve() != session_path:
        raise CheckpointGCError("Checkpoint GC plan targets another Session path")
    try:
        current_log_digest = hashlib.sha256(session_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CheckpointGCError(f"Session Log could not be read: {exc}") from exc
    if current_log_digest != plan.log_sha256:
        raise CheckpointGCError("Session Log changed after the GC plan was created")

    candidates: list[tuple[CheckpointGCItem, Path]] = []
    seen: set[str] = set()
    for item in plan.candidates:
        if item.relative_path in seen:
            raise CheckpointGCError("Checkpoint GC plan contains duplicate candidates")
        seen.add(item.relative_path)
        relative, path = _checkpoint_path(
            session_directory,
            checkpoint_directory,
            item.relative_path,
        )
        if item.protected or not _is_checkpoint_artifact_name(path.name):
            raise CheckpointGCError(
                f"Checkpoint GC plan contains an invalid candidate: {relative}"
            )
        try:
            payload = path.read_bytes()
            size = path.stat().st_size
        except OSError as exc:
            raise CheckpointGCError(
                f"Checkpoint GC candidate changed or disappeared: {relative}: {exc}"
            ) from exc
        digest = hashlib.sha256(payload).hexdigest()
        if size != item.size_bytes or digest != item.sha256:
            raise CheckpointGCError(
                f"Checkpoint GC candidate changed after planning: {relative}"
            )
        candidates.append((item, path))

    try:
        current_log_digest = hashlib.sha256(session_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CheckpointGCError(f"Session Log could not be re-read: {exc}") from exc
    if current_log_digest != plan.log_sha256:
        raise CheckpointGCError("Session Log changed during GC preflight")

    deleted_count = 0
    reclaimed_bytes = 0
    errors: list[CheckpointGCFailure] = []
    for item, path in candidates:
        try:
            path.unlink()
        except OSError as exc:
            errors.append(
                CheckpointGCFailure(
                    relative_path=item.relative_path,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            deleted_count += 1
            reclaimed_bytes += item.size_bytes
    return CheckpointGCReport(
        session_id=plan.session_id,
        session_path=plan.session_path,
        plan_created_at=plan.created_at,
        applied=True,
        kept_count=len(plan.kept),
        protected_count=len(plan.protected),
        missing_count=len(plan.missing),
        candidate_count=len(plan.candidates),
        estimated_bytes=plan.estimated_bytes,
        deleted_count=deleted_count,
        reclaimed_bytes=reclaimed_bytes,
        errors=tuple(errors),
    )


def _paths(manager: "SessionManager") -> tuple[Path, Path, Path]:
    manager._ensure_available()
    if manager.session_path is None:
        raise CheckpointGCError("Checkpoint GC requires a persistent Session")
    session_path = manager.session_path.resolve()
    session_directory = session_path.parent
    checkpoint_path = session_directory / "checkpoints"
    if checkpoint_path.is_symlink():
        raise CheckpointGCError(
            "Checkpoint directory cannot be a symbolic link"
        )
    checkpoint_directory = checkpoint_path.resolve()
    return session_path, session_directory, checkpoint_directory


def _checkpoint_path(
    session_directory: Path,
    checkpoint_directory: Path,
    relative_path: str,
) -> tuple[str, Path]:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise CheckpointGCError("Checkpoint path must be relative")
    if len(candidate.parts) != 2 or candidate.parts[0].lower() != "checkpoints":
        raise CheckpointGCError(
            f"Checkpoint path is not a direct Checkpoint artifact: {relative_path}"
        )
    lexical_path = session_directory / candidate
    if lexical_path.is_symlink():
        raise CheckpointGCError(
            f"Checkpoint artifact cannot be a symbolic link: {relative_path}"
        )
    path = lexical_path.resolve()
    try:
        path.relative_to(checkpoint_directory)
    except ValueError as exc:
        raise CheckpointGCError(
            f"Checkpoint path escapes the Checkpoint directory: {relative_path}"
        ) from exc
    if path.parent != checkpoint_directory:
        raise CheckpointGCError(
            f"Nested Checkpoint paths are not supported: {relative_path}"
        )
    normalized = path.relative_to(session_directory).as_posix()
    return normalized, path


def _is_checkpoint_artifact_name(name: str) -> bool:
    return bool(
        _CHECKPOINT_NAME.fullmatch(name)
        or _TEMPORARY_NAME.fullmatch(name)
    )


def _validate_checkpoint_projection(
    manager: "SessionManager",
    entry: CheckpointEntry,
    checkpoint: SessionCheckpoint,
) -> None:
    from evopi.session.session import (
        _checkpoint_messages_match,
        _project_messages,
        _project_plugin_state,
    )

    if (
        checkpoint.session_id != manager.session_id
        or checkpoint.checkpoint_id != entry.checkpoint_id
        or checkpoint.active_entry_id != entry.active_entry_id
    ):
        raise SessionFormatError(
            "Checkpoint identity does not match its Session Entry"
        )
    active_path = list(manager._path_to_entry(entry.active_entry_id))
    expected_messages = [
        message for _, message in _project_messages(active_path)
    ]
    if not _checkpoint_messages_match(checkpoint.messages, expected_messages):
        raise SessionFormatError(
            "Checkpoint messages do not match the Session path"
        )
    if checkpoint.plugin_state != _project_plugin_state(active_path):
        raise SessionFormatError(
            "Checkpoint Plugin state does not match the Session path"
        )


__all__ = [
    "CheckpointGCCategory",
    "CheckpointGCError",
    "CheckpointGCFailure",
    "CheckpointGCItem",
    "CheckpointGCPlan",
    "CheckpointGCReport",
    "CheckpointGCSettings",
    "DEFAULT_CHECKPOINT_GC_SETTINGS",
    "apply_checkpoint_gc",
    "plan_checkpoint_gc",
]
