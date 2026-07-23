"""Persistent Tree-ready Session manager."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal
from uuid import UUID

from evopi.core.messages import AssistantMessage, ToolResultMessage, UserMessage
from evopi.core.model_errors import ModelErrorInfo
from evopi.session.checkpoint import (
    SessionCheckpoint,
    SessionRunState,
    checkpoint_to_dict,
    load_checkpoint,
    write_checkpoint,
)
from evopi.session.errors import (
    SessionError,
    SessionFormatError,
    SessionLockError,
    SessionPersistenceError,
    SessionSerializationError,
)
from evopi.session.tree import (
    CheckpointEntry,
    MessageEntry,
    RunEndEntry,
    RunStartEntry,
    RuntimeFingerprint,
    SessionEntry,
    SessionHeader,
    SessionRunEndReason,
    entry_from_dict,
    entry_to_dict,
    header_from_dict,
    header_to_dict,
    json_value,
    new_id,
    utc_now,
)

_SESSION_FILENAME = "session.jsonl"
_CHECKPOINT_DIRECTORY = "checkpoints"
_LOCK_FILENAME = "session.lock"


@dataclass(slots=True, frozen=True, kw_only=True)
class SessionRecoveryInfo:
    reason: Literal["new", "continue", "open", "in_memory"]
    warnings: tuple[str, ...] = ()
    interrupted_run_id: str | None = None
    synthesized_tool_results: int = 0
    repaired_trailing_line: bool = False
    checkpoint_id: str | None = None
    rebuilt_from_log: bool = False
    workspace_mismatch: bool = False


@dataclass(slots=True, frozen=True, kw_only=True)
class SessionSummary:
    session_id: str
    workspace: str
    path: Path
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_run_reason: SessionRunEndReason | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "path": str(self.path),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "message_count": self.message_count,
            "last_run_reason": self.last_run_reason,
            "error": self.error,
        }


@dataclass(slots=True)
class _RecoveryState:
    reason: Literal["new", "continue", "open", "in_memory"]
    warnings: list[str] = field(default_factory=list)
    interrupted_run_id: str | None = None
    synthesized_tool_results: int = 0
    repaired_trailing_line: bool = False
    checkpoint_id: str | None = None
    rebuilt_from_log: bool = False
    workspace_mismatch: bool = False

    def freeze(self) -> SessionRecoveryInfo:
        return SessionRecoveryInfo(
            reason=self.reason,
            warnings=tuple(self.warnings),
            interrupted_run_id=self.interrupted_run_id,
            synthesized_tool_results=self.synthesized_tool_results,
            repaired_trailing_line=self.repaired_trailing_line,
            checkpoint_id=self.checkpoint_id,
            rebuilt_from_log=self.rebuilt_from_log,
            workspace_mismatch=self.workspace_mismatch,
        )


class _SessionFileLock:
    """Lifetime, non-blocking OS lock released automatically on process exit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import importlib

                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, PermissionError) as exc:
            handle.close()
            raise SessionLockError(
                f"Session is already open in another process: {self.path.parent}"
            ) from exc
        self._handle = handle
        _make_private(self.path, directory=False)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import importlib

                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


class SessionManager:
    """Own one in-memory or persistent Session and its active Tree leaf."""

    def __init__(self, workspace: str | Path | None = None) -> None:
        normalized_workspace = normalize_workspace(workspace or Path.cwd())
        self._attached_workspace = normalized_workspace
        self.header = SessionHeader(
            session_id=new_id(),
            workspace=normalized_workspace,
        )
        self.root: Path | None = None
        self.session_path: Path | None = None
        self._entries: list[SessionEntry] = []
        self._entry_index: dict[str, SessionEntry] = {}
        self._messages: list[UserMessage | AssistantMessage | ToolResultMessage] = []
        self._last_checkpoint: SessionCheckpoint | None = None
        self._lock: _SessionFileLock | None = None
        self._closed = False
        self._broken = False
        self._recovery = _RecoveryState(reason="in_memory")

    @classmethod
    def in_memory(
        cls, workspace: str | Path | None = None
    ) -> "SessionManager":
        return cls(workspace)

    @classmethod
    def create(
        cls,
        workspace: str | Path,
        *,
        root: str | Path | None = None,
    ) -> "SessionManager":
        manager = cls(workspace)
        manager.root = resolve_session_root(root)
        session_directory = (
            manager.root
            / workspace_bucket(manager.header.workspace)
            / manager.header.session_id
        )
        session_directory.mkdir(parents=True, exist_ok=False)
        _make_private(manager.root, directory=True)
        _make_private(session_directory.parent, directory=True)
        _make_private(session_directory, directory=True)
        manager.session_path = session_directory / _SESSION_FILENAME
        manager._lock = _SessionFileLock(session_directory / _LOCK_FILENAME)
        manager._lock.acquire()
        try:
            manager._write_header()
        except Exception:
            manager.close()
            raise
        manager._recovery = _RecoveryState(reason="new")
        return manager

    @classmethod
    def continue_recent(
        cls,
        workspace: str | Path,
        *,
        root: str | Path | None = None,
    ) -> "SessionManager":
        summaries = cls.list(workspace=workspace, root=root)
        valid = [summary for summary in summaries if summary.error is None]
        if not valid:
            return cls.create(workspace, root=root)
        manager = cls.open(valid[0].path, workspace=workspace, root=root)
        manager._recovery.reason = "continue"
        return manager

    @classmethod
    def open(
        cls,
        reference: str | Path,
        *,
        workspace: str | Path,
        root: str | Path | None = None,
    ) -> "SessionManager":
        session_root = resolve_session_root(root)
        path = _resolve_session_reference(reference, session_root)
        manager = cls(workspace)
        manager.root = session_root
        manager.session_path = path
        manager._lock = _SessionFileLock(path.parent / _LOCK_FILENAME)
        manager._lock.acquire()
        manager._recovery = _RecoveryState(reason="open")
        try:
            header, entries, repaired = _read_session_file(path, repair=True)
            manager.header = header
            manager._entries = entries
            manager._entry_index = {entry.entry_id: entry for entry in entries}
            manager._recovery.repaired_trailing_line = repaired
            if repaired:
                manager._recovery.warnings.append(
                    "The incomplete trailing Session Log record was removed"
                )
            current_workspace = normalize_workspace(workspace)
            if current_workspace != header.workspace:
                manager._recovery.workspace_mismatch = True
                manager._recovery.warnings.append(
                    "Session workspace differs from the current Harness workspace: "
                    f"{header.workspace} -> {current_workspace}"
                )
            manager._restore_messages()
            manager._recover_interrupted_run()
            manager._ensure_latest_run_checkpoint()
        except Exception:
            manager.close()
            raise
        return manager

    @classmethod
    def list(
        cls,
        *,
        workspace: str | Path | None = None,
        root: str | Path | None = None,
    ) -> list[SessionSummary]:
        session_root = resolve_session_root(root)
        if not session_root.exists():
            return []
        if workspace is None:
            candidates = session_root.glob(f"*/*/{_SESSION_FILENAME}")
        else:
            bucket = session_root / workspace_bucket(normalize_workspace(workspace))
            candidates = bucket.glob(f"*/{_SESSION_FILENAME}")
        summaries = [_read_summary(path) for path in candidates]
        return sorted(summaries, key=lambda item: item.updated_at, reverse=True)

    @property
    def session_id(self) -> str:
        return self.header.session_id

    @property
    def workspace(self) -> str:
        return self.header.workspace

    @property
    def attached_workspace(self) -> str:
        return self._attached_workspace

    @property
    def is_persistent(self) -> bool:
        return self.session_path is not None

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def leaf_id(self) -> str | None:
        return self._entries[-1].entry_id if self._entries else None

    @property
    def entries(self) -> tuple[SessionEntry, ...]:
        return tuple(self._entries)

    @property
    def messages(
        self,
    ) -> tuple[UserMessage | AssistantMessage | ToolResultMessage, ...]:
        return tuple(self._messages)

    @property
    def last_checkpoint(self) -> SessionCheckpoint | None:
        return self._last_checkpoint

    @property
    def recovery_info(self) -> SessionRecoveryInfo:
        return self._recovery.freeze()

    @property
    def is_broken(self) -> bool:
        return self._broken

    @property
    def last_runtime_fingerprint(self) -> RuntimeFingerprint | None:
        if self._last_checkpoint is not None:
            return self._last_checkpoint.runtime_fingerprint
        return next(
            (
                entry.runtime_fingerprint
                for entry in reversed(self.get_active_path())
                if isinstance(entry, RunStartEntry)
            ),
            None,
        )

    def compare_runtime(
        self, current: RuntimeFingerprint
    ) -> tuple[str, ...]:
        previous = self.last_runtime_fingerprint
        if previous is None:
            return ()
        changed = previous.changed_components(current)
        if changed:
            warning = (
                "Session runtime differs from the current Harness: "
                + ", ".join(changed)
            )
            if warning not in self._recovery.warnings:
                self._recovery.warnings.append(warning)
        return changed

    def new_session(self) -> "SessionManager":
        self._ensure_available()
        if self.is_persistent:
            replacement = SessionManager.create(
                self.attached_workspace,
                root=self.root,
            )
        else:
            replacement = SessionManager.in_memory(self.attached_workspace)
        self.close()
        return replacement

    def append_run_start(
        self,
        *,
        run_id: str,
        runtime_fingerprint: RuntimeFingerprint,
        trace_path: str | Path | None = None,
    ) -> RunStartEntry:
        self._ensure_available()
        if self._open_run() is not None:
            raise SessionPersistenceError(
                "Cannot start a new Run while the Session contains an open Run"
            )
        entry = RunStartEntry(
            entry_id=new_id(),
            parent_id=self.leaf_id,
            run_id=_normalize_uuid(run_id, "run_id"),
            runtime_fingerprint=runtime_fingerprint,
            trace_path=str(Path(trace_path).resolve()) if trace_path is not None else None,
        )
        self._append_entry(entry)
        return entry

    def append_message(
        self,
        *,
        run_id: str,
        message: UserMessage | AssistantMessage | ToolResultMessage,
    ) -> MessageEntry:
        self._ensure_available()
        normalized_run_id = _normalize_uuid(run_id, "run_id")
        open_run = self._open_run()
        if open_run is None or open_run.run_id != normalized_run_id:
            raise SessionPersistenceError(
                "Messages must belong to the currently open Session Run"
            )
        entry = MessageEntry(
            entry_id=new_id(),
            parent_id=self.leaf_id,
            run_id=normalized_run_id,
            message=message,
        )
        self._append_entry(entry)
        return entry

    def append_run_end(
        self,
        *,
        run_id: str,
        reason: SessionRunEndReason,
        error: str | None = None,
        error_info: ModelErrorInfo | None = None,
        recovered: bool = False,
    ) -> RunEndEntry:
        self._ensure_available()
        normalized_run_id = _normalize_uuid(run_id, "run_id")
        open_run = self._open_run()
        if open_run is None or open_run.run_id != normalized_run_id:
            raise SessionPersistenceError(
                "run_end must close the currently open Session Run"
            )
        entry = RunEndEntry(
            entry_id=new_id(),
            parent_id=self.leaf_id,
            run_id=normalized_run_id,
            reason=reason,
            error=error,
            error_info=error_info,
            recovered=recovered,
        )
        self._append_entry(entry)
        return entry

    def create_checkpoint(
        self,
        *,
        run_end: RunEndEntry,
        runtime_fingerprint: RuntimeFingerprint,
    ) -> SessionCheckpoint | None:
        self._ensure_available()
        if self.leaf_id != run_end.entry_id:
            raise SessionPersistenceError(
                "Checkpoint must immediately follow its run_end"
            )
        checkpoint_id = new_id()
        log_offset = (
            self.session_path.stat().st_size
            if self.session_path is not None
            else len(self._entries)
        )
        checkpoint = SessionCheckpoint(
            checkpoint_id=checkpoint_id,
            session_id=self.session_id,
            active_entry_id=run_end.entry_id,
            log_offset=log_offset,
            messages=tuple(self._messages),
            last_run=SessionRunState(
                run_id=run_end.run_id,
                reason=run_end.reason,
                error=run_end.error,
                error_info=run_end.error_info,
            ),
            runtime_fingerprint=runtime_fingerprint,
        )
        if self.session_path is None:
            payload = json.dumps(
                checkpoint_to_dict(checkpoint),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            relative_path = ""
        else:
            relative_path = (
                f"{_CHECKPOINT_DIRECTORY}/{checkpoint_id}.json"
            )
            checkpoint_path = self.session_path.parent / relative_path
            try:
                digest = write_checkpoint(checkpoint_path, checkpoint)
            except SessionPersistenceError as exc:
                self._recovery.warnings.append(str(exc))
                return None
        entry = CheckpointEntry(
            entry_id=new_id(),
            parent_id=self.leaf_id,
            run_id=run_end.run_id,
            checkpoint_id=checkpoint_id,
            path=relative_path,
            sha256=digest,
            active_entry_id=run_end.entry_id,
        )
        self._append_entry(entry)
        self._last_checkpoint = checkpoint
        self._recovery.checkpoint_id = checkpoint_id
        return checkpoint

    def get_entry(self, entry_id: str) -> SessionEntry:
        try:
            return self._entry_index[_normalize_uuid(entry_id, "entry_id")]
        except KeyError as exc:
            raise KeyError(f"Session entry '{entry_id}' does not exist") from exc

    def get_children(self, parent_id: str | None) -> tuple[SessionEntry, ...]:
        normalized = (
            _normalize_uuid(parent_id, "parent_id")
            if parent_id is not None
            else None
        )
        return tuple(
            entry for entry in self._entries if entry.parent_id == normalized
        )

    def get_active_path(self) -> tuple[SessionEntry, ...]:
        if self.leaf_id is None:
            return ()
        path: list[SessionEntry] = []
        current: str | None = self.leaf_id
        seen: set[str] = set()
        while current is not None:
            if current in seen:
                raise SessionFormatError("Session Tree contains a parent cycle")
            seen.add(current)
            try:
                entry = self._entry_index[current]
            except KeyError as exc:
                raise SessionFormatError(
                    f"Session Tree references missing parent {current}"
                ) from exc
            path.append(entry)
            current = entry.parent_id
        path.reverse()
        return tuple(path)

    def reset(self) -> None:
        """Reset an in-memory Session; persistent callers must attach a new manager."""

        self._ensure_available()
        if self.is_persistent:
            raise SessionError(
                "Persistent Session reset requires creating and attaching a new Session"
            )
        self.header = SessionHeader(
            session_id=new_id(),
            workspace=self.workspace,
        )
        self._entries.clear()
        self._entry_index.clear()
        self._messages.clear()
        self._last_checkpoint = None
        self._recovery = _RecoveryState(reason="in_memory")
        self._broken = False

    def close(self) -> None:
        if self._closed:
            return
        if self._lock is not None:
            self._lock.release()
        self._closed = True

    def __enter__(self) -> "SessionManager":
        self._ensure_available()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _write_header(self) -> None:
        if self.session_path is None:
            return
        payload = _json_line(header_to_dict(self.header))
        try:
            with self.session_path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _make_private(self.session_path, directory=False)
        except OSError as exc:
            self._broken = True
            raise SessionPersistenceError(
                f"Session header could not be written: {exc}"
            ) from exc

    def _append_entry(self, entry: SessionEntry) -> None:
        if entry.entry_id in self._entry_index:
            raise SessionPersistenceError(
                f"Duplicate Session entry ID: {entry.entry_id}"
            )
        if entry.parent_id != self.leaf_id:
            raise SessionPersistenceError(
                "New Session entries must extend the current active leaf"
            )
        try:
            payload = _json_line(entry_to_dict(entry))
            if self.session_path is not None:
                with self.session_path.open("ab") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
        except (OSError, SessionSerializationError) as exc:
            self._broken = True
            if isinstance(exc, SessionSerializationError):
                raise
            raise SessionPersistenceError(
                f"Session Log entry could not be written: {exc}"
            ) from exc
        self._entries.append(entry)
        self._entry_index[entry.entry_id] = entry
        if isinstance(entry, MessageEntry):
            self._messages.append(entry.message)

    def _ensure_available(self) -> None:
        if self._closed:
            raise SessionError("SessionManager is closed")
        if self._broken:
            raise SessionPersistenceError(
                "SessionManager cannot continue after a persistence failure"
            )

    def _open_run(self) -> RunStartEntry | None:
        open_run: RunStartEntry | None = None
        for entry in self.get_active_path():
            if isinstance(entry, RunStartEntry):
                if open_run is not None:
                    raise SessionFormatError("Session contains nested open Runs")
                open_run = entry
            elif isinstance(entry, (MessageEntry, RunEndEntry)):
                if open_run is None or open_run.run_id != entry.run_id:
                    raise SessionFormatError(
                        f"{entry.type} does not belong to the active Run"
                    )
                if isinstance(entry, RunEndEntry):
                    open_run = None
            elif open_run is not None:
                raise SessionFormatError(
                    "Checkpoint cannot be written before run_end"
                )
        return open_run

    def _restore_messages(self) -> None:
        path = list(self.get_active_path())
        messages: list[UserMessage | AssistantMessage | ToolResultMessage] = []
        checkpoint: SessionCheckpoint | None = None
        replay_start = 0
        checkpoint_entries = [
            (index, entry)
            for index, entry in enumerate(path)
            if isinstance(entry, CheckpointEntry)
        ]
        if self.session_path is not None:
            for index, checkpoint_entry in reversed(checkpoint_entries):
                try:
                    checkpoint_path = _safe_checkpoint_path(
                        self.session_path.parent, checkpoint_entry.path
                    )
                    candidate = load_checkpoint(
                        checkpoint_path,
                        expected_sha256=checkpoint_entry.sha256,
                    )
                    if candidate.session_id != self.session_id:
                        raise SessionFormatError(
                            "Checkpoint belongs to another Session"
                        )
                    if candidate.checkpoint_id != checkpoint_entry.checkpoint_id:
                        raise SessionFormatError(
                            "Checkpoint ID does not match its Session entry"
                        )
                    if candidate.active_entry_id != checkpoint_entry.active_entry_id:
                        raise SessionFormatError(
                            "Checkpoint active Entry does not match its Session entry"
                        )
                    active_index = next(
                        (
                            item_index
                            for item_index, item in enumerate(path)
                            if item.entry_id == candidate.active_entry_id
                        ),
                        None,
                    )
                    if active_index is None or active_index >= index:
                        raise SessionFormatError(
                            "Checkpoint active Entry is not an ancestor"
                        )
                    checkpoint = candidate
                    messages = list(candidate.messages)
                    replay_start = active_index + 1
                    break
                except SessionFormatError as exc:
                    self._recovery.warnings.append(str(exc))
        if checkpoint is None:
            self._recovery.rebuilt_from_log = True
            replay_start = 0
        else:
            self._last_checkpoint = checkpoint
            self._recovery.checkpoint_id = checkpoint.checkpoint_id
        for replay_entry in path[replay_start:]:
            if isinstance(replay_entry, MessageEntry):
                messages.append(replay_entry.message)
        self._messages = messages

    def _recover_interrupted_run(self) -> None:
        open_run = self._open_run()
        if open_run is None:
            return
        path = self.get_active_path()
        run_messages = [
            entry.message
            for entry in path
            if isinstance(entry, MessageEntry) and entry.run_id == open_run.run_id
        ]
        completed_call_ids = {
            message.tool_call_id
            for message in run_messages
            if isinstance(message, ToolResultMessage)
        }
        missing_calls = [
            call
            for message in run_messages
            if isinstance(message, AssistantMessage)
            for call in message.tool_calls
            if call.id not in completed_call_ids
        ]
        for call in missing_calls:
            self.append_message(
                run_id=open_run.run_id,
                message=ToolResultMessage(
                    content=(
                        "Session recovery: this tool call has no recorded result. "
                        "Its execution outcome is unknown and EvoPi did not run it again."
                    ),
                    tool_call_id=call.id,
                    tool_name=call.name,
                    is_error=True,
                    metadata={
                        "session_recovery": True,
                        "outcome": "unknown",
                        "original_run_id": open_run.run_id,
                    },
                ),
            )
        run_end = self.append_run_end(
            run_id=open_run.run_id,
            reason="interrupted",
            error="Session ended before the Run produced run_end",
            recovered=True,
        )
        self._recovery.interrupted_run_id = open_run.run_id
        self._recovery.synthesized_tool_results = len(missing_calls)
        self._recovery.warnings.append(
            f"Recovered interrupted Run {open_run.run_id}; "
            f"synthesized {len(missing_calls)} unknown ToolResult message(s)"
        )
        self.create_checkpoint(
            run_end=run_end,
            runtime_fingerprint=open_run.runtime_fingerprint,
        )

    def _ensure_latest_run_checkpoint(self) -> None:
        if self._open_run() is not None:
            return
        path = self.get_active_path()
        last_end = next(
            (entry for entry in reversed(path) if isinstance(entry, RunEndEntry)),
            None,
        )
        if last_end is None:
            return
        if (
            self._last_checkpoint is not None
            and self._last_checkpoint.last_run.run_id == last_end.run_id
        ):
            return
        run_start = next(
            (
                entry
                for entry in reversed(path)
                if isinstance(entry, RunStartEntry)
                and entry.run_id == last_end.run_id
            ),
            None,
        )
        if run_start is None:
            raise SessionFormatError(
                f"run_end {last_end.run_id} has no matching run_start"
            )
        # A stale checkpoint entry is non-context metadata. Extend the current leaf
        # with a recovery run boundary only when needed so Checkpoint remains
        # immediately after a run_end.
        if self.leaf_id != last_end.entry_id:
            return
        self.create_checkpoint(
            run_end=last_end,
            runtime_fingerprint=run_start.runtime_fingerprint,
        )


def resolve_session_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    configured = os.environ.get("EVOPI_SESSION_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".evopi" / "sessions").resolve()


def normalize_workspace(workspace: str | Path) -> str:
    return os.path.normcase(str(Path(workspace).expanduser().resolve()))


def workspace_bucket(workspace: str | Path) -> str:
    normalized = normalize_workspace(workspace)
    name = Path(normalized).name or "workspace"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._") or "workspace"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{slug[:48]}-{digest}"


def build_runtime_fingerprint(
    *,
    harness: str,
    model: str,
    system_prompt: str,
    tools: list[dict[str, Any]],
    policies: list[dict[str, Any]],
) -> RuntimeFingerprint:
    return RuntimeFingerprint(
        harness=harness,
        model=model,
        system_prompt_sha256=_hash_json(system_prompt),
        tools_sha256=_hash_json(sorted(tools, key=lambda item: str(item.get("name", item)))),
        policies_sha256=_hash_json(
            sorted(policies, key=lambda item: str(item.get("name", item)))
        ),
    )


def _hash_json(value: Any) -> str:
    canonical = json.dumps(
        json_value(value, path="fingerprint"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _read_session_file(
    path: Path,
    *,
    repair: bool,
) -> tuple[SessionHeader, list[SessionEntry], bool]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SessionFormatError(f"Session Log could not be read: {exc}") from exc
    if not payload:
        raise SessionFormatError("Session Log is empty")

    records: list[tuple[int, dict[str, Any]]] = []
    repaired = False
    offset = 0
    lines = payload.splitlines(keepends=True)
    nonempty_indices = [
        index for index, raw_line in enumerate(lines) if raw_line.strip()
    ]
    last_nonempty = nonempty_indices[-1] if nonempty_indices else -1
    for index, raw_line in enumerate(lines):
        line_number = index + 1
        start_offset = offset
        offset += len(raw_line)
        if not raw_line.strip():
            continue
        try:
            decoded = raw_line.decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            is_trailing_residue = (
                index == last_nonempty
                and not payload.endswith((b"\n", b"\r"))
            )
            if repair and is_trailing_residue:
                try:
                    with path.open("r+b") as handle:
                        handle.truncate(start_offset)
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError as write_error:
                    raise SessionPersistenceError(
                        f"Trailing Session record could not be repaired: {write_error}"
                    ) from write_error
                repaired = True
                break
            raise SessionFormatError(
                "record is not valid UTF-8 JSON", line_number=line_number
            ) from exc
        if not isinstance(value, dict):
            raise SessionFormatError(
                "record must be an object", line_number=line_number
            )
        records.append((line_number, value))
    if not records:
        raise SessionFormatError("Session Log has no records")
    try:
        header = header_from_dict(records[0][1])
    except SessionFormatError as exc:
        raise SessionFormatError(exc.reason, line_number=records[0][0]) from exc

    entries: list[SessionEntry] = []
    known_ids: set[str] = set()
    for line_number, value in records[1:]:
        try:
            entry = entry_from_dict(value)
        except SessionFormatError as exc:
            raise SessionFormatError(exc.reason, line_number=line_number) from exc
        if entry.entry_id in known_ids:
            raise SessionFormatError(
                f"duplicate entry_id {entry.entry_id}", line_number=line_number
            )
        if entry.parent_id is None:
            if entries:
                raise SessionFormatError(
                    "only the first Tree entry may have parent_id null",
                    line_number=line_number,
                )
        elif entry.parent_id not in known_ids:
            raise SessionFormatError(
                f"parent_id {entry.parent_id} does not reference an earlier entry",
                line_number=line_number,
            )
        if isinstance(entry, CheckpointEntry):
            if entry.active_entry_id not in known_ids:
                raise SessionFormatError(
                    "checkpoint active_entry_id is not an earlier entry",
                    line_number=line_number,
                )
        known_ids.add(entry.entry_id)
        entries.append(entry)
    return header, entries, repaired


def _read_summary(path: Path) -> SessionSummary:
    try:
        header, entries, _ = _read_session_file(path, repair=False)
        message_count = sum(isinstance(entry, MessageEntry) for entry in entries)
        last_end = next(
            (
                entry
                for entry in reversed(entries)
                if isinstance(entry, RunEndEntry)
            ),
            None,
        )
        updated_at = (
            entries[-1].created_at if entries else header.created_at
        )
        return SessionSummary(
            session_id=header.session_id,
            workspace=header.workspace,
            path=path,
            created_at=header.created_at,
            updated_at=updated_at,
            message_count=message_count,
            last_run_reason=last_end.reason if last_end is not None else None,
        )
    except SessionError as exc:
        timestamp = datetime.fromtimestamp(path.stat().st_mtime, tz=utc_now().tzinfo)
        return SessionSummary(
            session_id=path.parent.name,
            workspace="",
            path=path,
            created_at=timestamp,
            updated_at=timestamp,
            message_count=0,
            last_run_reason=None,
            error=str(exc),
        )


def _resolve_session_reference(reference: str | Path, root: Path) -> Path:
    candidate = Path(reference).expanduser()
    if candidate.exists():
        path = candidate / _SESSION_FILENAME if candidate.is_dir() else candidate
        return path.resolve()
    session_id = _normalize_uuid(str(reference), "session_id")
    matches = list(root.glob(f"*/*/{_SESSION_FILENAME}"))
    matches = [
        path
        for path in matches
        if path.parent.name == session_id
    ]
    if not matches:
        raise SessionError(f"Session '{session_id}' was not found")
    if len(matches) > 1:
        raise SessionError(f"Session ID '{session_id}' is ambiguous")
    return matches[0].resolve()


def _safe_checkpoint_path(session_directory: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise SessionFormatError("Checkpoint path must be relative")
    resolved = (session_directory / candidate).resolve()
    try:
        resolved.relative_to(session_directory.resolve())
    except ValueError as exc:
        raise SessionFormatError("Checkpoint path escapes the Session directory") from exc
    return resolved


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return UUID(value).hex
    except ValueError as exc:
        raise SessionFormatError(f"{field_name} must be a UUID") from exc


def _json_line(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _make_private(path: Path, *, directory: bool) -> None:
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        pass


__all__ = [
    "SessionManager",
    "SessionRecoveryInfo",
    "SessionSummary",
    "build_runtime_fingerprint",
    "normalize_workspace",
    "resolve_session_root",
    "workspace_bucket",
]
