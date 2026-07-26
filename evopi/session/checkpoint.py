"""Immutable materialized Session checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from evopi.core.messages import AssistantMessage, ToolResultMessage, UserMessage
from evopi.core.model_errors import ModelErrorInfo
from evopi.session.errors import (
    SessionFormatError,
    SessionPersistenceError,
)
from evopi.session.tree import (
    SESSION_SCHEMA_VERSION,
    RuntimeFingerprint,
    SessionRunEndReason,
    fingerprint_from_dict,
    fingerprint_to_dict,
    message_from_dict,
    message_to_dict,
    model_error_info_from_dict,
    model_error_info_to_dict,
    json_value,
    utc_now,
)


@dataclass(slots=True, frozen=True, kw_only=True)
class SessionRunState:
    run_id: str
    reason: SessionRunEndReason
    error: str | None = None
    error_info: ModelErrorInfo | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class SessionCheckpoint:
    checkpoint_id: str
    session_id: str
    active_entry_id: str
    log_offset: int
    messages: tuple[UserMessage | AssistantMessage | ToolResultMessage, ...]
    last_run: SessionRunState
    runtime_fingerprint: RuntimeFingerprint
    plugin_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = SESSION_SCHEMA_VERSION


def checkpoint_to_dict(checkpoint: SessionCheckpoint) -> dict[str, Any]:
    return {
        "schema_version": checkpoint.schema_version,
        "type": "checkpoint_snapshot",
        "checkpoint_id": checkpoint.checkpoint_id,
        "session_id": checkpoint.session_id,
        "active_entry_id": checkpoint.active_entry_id,
        "log_offset": checkpoint.log_offset,
        "created_at": checkpoint.created_at.isoformat(),
        "messages": [message_to_dict(message) for message in checkpoint.messages],
        "last_run": {
            "run_id": checkpoint.last_run.run_id,
            "reason": checkpoint.last_run.reason,
            "error": checkpoint.last_run.error,
            "error_info": (
                model_error_info_to_dict(checkpoint.last_run.error_info)
                if checkpoint.last_run.error_info is not None
                else None
            ),
        },
        "runtime_fingerprint": fingerprint_to_dict(
            checkpoint.runtime_fingerprint
        ),
        "plugin_state": json_value(
            checkpoint.plugin_state,
            path="checkpoint.plugin_state",
        ),
    }


def checkpoint_from_dict(value: Mapping[str, Any]) -> SessionCheckpoint:
    from evopi.session.tree import (  # Local import keeps validation helpers private.
        _require_datetime,
        _require_id,
        _require_mapping,
    )

    if value.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise SessionFormatError(
            f"unsupported Checkpoint schema_version "
            f"{value.get('schema_version')!r}"
        )
    if value.get("type") != "checkpoint_snapshot":
        raise SessionFormatError("Checkpoint type must be checkpoint_snapshot")
    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list):
        raise SessionFormatError("Checkpoint messages must be an array")
    messages: list[UserMessage | AssistantMessage | ToolResultMessage] = []
    for raw_message in raw_messages:
        message = message_from_dict(_require_mapping(raw_message, "checkpoint.message"))
        if not isinstance(
            message, (UserMessage, AssistantMessage, ToolResultMessage)
        ):
            raise SessionFormatError(
                "Checkpoint cannot contain a SystemMessage"
            )
        messages.append(message)

    raw_last_run = _require_mapping(value.get("last_run"), "last_run")
    reason = raw_last_run.get("reason")
    if reason not in {
        "completed",
        "terminated",
        "aborted",
        "error",
        "turn_limit",
        "deadline_exceeded",
        "interrupted",
    }:
        raise SessionFormatError("Checkpoint contains an invalid Run reason")
    error = raw_last_run.get("error")
    if error is not None and not isinstance(error, str):
        raise SessionFormatError("Checkpoint Run error must be a string or null")
    raw_error_info = raw_last_run.get("error_info")
    error_info = (
        model_error_info_from_dict(
            _require_mapping(raw_error_info, "last_run.error_info")
        )
        if raw_error_info is not None
        else None
    )
    log_offset = value.get("log_offset")
    if not isinstance(log_offset, int) or isinstance(log_offset, bool) or log_offset < 0:
        raise SessionFormatError(
            "Checkpoint log_offset must be a non-negative integer"
        )
    raw_plugin_state = _require_mapping(
        value.get("plugin_state", {}),
        "plugin_state",
    )
    plugin_state: dict[str, dict[str, Any]] = {}
    for plugin_name, raw_values in raw_plugin_state.items():
        plugin_state[plugin_name] = dict(
            _require_mapping(raw_values, f"plugin_state.{plugin_name}")
        )
    return SessionCheckpoint(
        checkpoint_id=_require_id(
            value.get("checkpoint_id"), "checkpoint_id"
        ),
        session_id=_require_id(value.get("session_id"), "session_id"),
        active_entry_id=_require_id(
            value.get("active_entry_id"), "active_entry_id"
        ),
        log_offset=log_offset,
        created_at=_require_datetime(value.get("created_at"), "created_at"),
        messages=tuple(messages),
        last_run=SessionRunState(
            run_id=_require_id(raw_last_run.get("run_id"), "last_run.run_id"),
            reason=reason,
            error=error,
            error_info=error_info,
        ),
        runtime_fingerprint=fingerprint_from_dict(
            _require_mapping(
                value.get("runtime_fingerprint"), "runtime_fingerprint"
            )
        ),
        plugin_state=plugin_state,
    )


def write_checkpoint(path: Path, checkpoint: SessionCheckpoint) -> str:
    """Atomically write one immutable snapshot and return its SHA-256."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _make_private(path.parent, directory=True)
    payload = json.dumps(
        checkpoint_to_dict(checkpoint),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        _make_private(temporary, directory=False)
        os.replace(temporary, path)
        _make_private(path, directory=False)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SessionPersistenceError(
            f"Checkpoint could not be written: {exc}"
        ) from exc
    return digest


def load_checkpoint(path: Path, *, expected_sha256: str) -> SessionCheckpoint:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SessionFormatError(f"Checkpoint could not be read: {exc}") from exc
    canonical = payload.rstrip(b"\r\n")
    actual = hashlib.sha256(canonical).hexdigest()
    if actual != expected_sha256:
        raise SessionFormatError(
            f"Checkpoint checksum mismatch: expected {expected_sha256}, got {actual}"
        )
    try:
        value = json.loads(canonical)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionFormatError("Checkpoint is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SessionFormatError("Checkpoint root must be an object")
    return checkpoint_from_dict(value)


def _make_private(path: Path, *, directory: bool) -> None:
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        # Windows ACLs are not faithfully represented by chmod. Best effort only.
        pass


__all__ = [
    "SessionCheckpoint",
    "SessionRunState",
    "checkpoint_from_dict",
    "checkpoint_to_dict",
    "load_checkpoint",
    "write_checkpoint",
]
