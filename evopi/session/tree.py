"""Versioned Session Tree records and strict message codecs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from uuid import UUID, uuid4

from evopi.core.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from evopi.core.model_errors import ModelErrorInfo, ModelErrorKind
from evopi.core.tool import ToolCall
from evopi.session.errors import SessionFormatError, SessionSerializationError

SESSION_SCHEMA_VERSION = 1

SessionRunEndReason: TypeAlias = Literal[
    "completed",
    "terminated",
    "aborted",
    "error",
    "turn_limit",
    "interrupted",
]
SessionEntryType: TypeAlias = Literal[
    "run_start",
    "message",
    "run_end",
    "checkpoint",
]


def new_id() -> str:
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True, frozen=True, kw_only=True)
class RuntimeFingerprint:
    harness: str
    model: str
    system_prompt_sha256: str
    tools_sha256: str
    policies_sha256: str

    def changed_components(self, other: "RuntimeFingerprint") -> tuple[str, ...]:
        changed: list[str] = []
        for name in (
            "harness",
            "model",
            "system_prompt_sha256",
            "tools_sha256",
            "policies_sha256",
        ):
            if getattr(self, name) != getattr(other, name):
                changed.append(name)
        return tuple(changed)


@dataclass(slots=True, frozen=True, kw_only=True)
class SessionHeader:
    session_id: str
    workspace: str
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = SESSION_SCHEMA_VERSION
    type: Literal["session"] = field(default="session", init=False)


@dataclass(slots=True, frozen=True, kw_only=True)
class RunStartEntry:
    entry_id: str
    parent_id: str | None
    run_id: str
    runtime_fingerprint: RuntimeFingerprint
    trace_path: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = SESSION_SCHEMA_VERSION
    type: Literal["run_start"] = field(default="run_start", init=False)


@dataclass(slots=True, frozen=True, kw_only=True)
class MessageEntry:
    entry_id: str
    parent_id: str | None
    run_id: str
    message: UserMessage | AssistantMessage | ToolResultMessage
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = SESSION_SCHEMA_VERSION
    type: Literal["message"] = field(default="message", init=False)


@dataclass(slots=True, frozen=True, kw_only=True)
class RunEndEntry:
    entry_id: str
    parent_id: str | None
    run_id: str
    reason: SessionRunEndReason
    error: str | None = None
    error_info: ModelErrorInfo | None = None
    recovered: bool = False
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = SESSION_SCHEMA_VERSION
    type: Literal["run_end"] = field(default="run_end", init=False)


@dataclass(slots=True, frozen=True, kw_only=True)
class CheckpointEntry:
    entry_id: str
    parent_id: str | None
    run_id: str
    checkpoint_id: str
    path: str
    sha256: str
    active_entry_id: str
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = SESSION_SCHEMA_VERSION
    type: Literal["checkpoint"] = field(default="checkpoint", init=False)


SessionEntry: TypeAlias = (
    RunStartEntry | MessageEntry | RunEndEntry | CheckpointEntry
)


def header_to_dict(header: SessionHeader) -> dict[str, Any]:
    return {
        "schema_version": header.schema_version,
        "type": header.type,
        "session_id": header.session_id,
        "created_at": header.created_at.isoformat(),
        "workspace": header.workspace,
    }


def header_from_dict(value: Mapping[str, Any]) -> SessionHeader:
    _require_version(value)
    if value.get("type") != "session":
        raise SessionFormatError("the first record must be a Session header")
    session_id = _require_id(value.get("session_id"), "session_id")
    workspace = _require_string(value.get("workspace"), "workspace")
    return SessionHeader(
        session_id=session_id,
        workspace=workspace,
        created_at=_require_datetime(value.get("created_at"), "created_at"),
    )


def entry_to_dict(entry: SessionEntry) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": entry.schema_version,
        "type": entry.type,
        "entry_id": entry.entry_id,
        "parent_id": entry.parent_id,
        "created_at": entry.created_at.isoformat(),
        "run_id": entry.run_id,
    }
    if isinstance(entry, RunStartEntry):
        base["runtime_fingerprint"] = fingerprint_to_dict(entry.runtime_fingerprint)
        base["trace_path"] = entry.trace_path
    elif isinstance(entry, MessageEntry):
        base["message"] = message_to_dict(entry.message)
    elif isinstance(entry, RunEndEntry):
        base.update(
            {
                "reason": entry.reason,
                "error": entry.error,
                "error_info": (
                    model_error_info_to_dict(entry.error_info)
                    if entry.error_info is not None
                    else None
                ),
                "recovered": entry.recovered,
            }
        )
    else:
        base.update(
            {
                "checkpoint_id": entry.checkpoint_id,
                "path": entry.path,
                "sha256": entry.sha256,
                "active_entry_id": entry.active_entry_id,
            }
        )
    return base


def entry_from_dict(value: Mapping[str, Any]) -> SessionEntry:
    _require_version(value)
    entry_type = value.get("type")
    entry_id = _require_id(value.get("entry_id"), "entry_id")
    parent_id = _optional_id(value.get("parent_id"), "parent_id")
    created_at = _require_datetime(value.get("created_at"), "created_at")
    run_id = _require_id(value.get("run_id"), "run_id")
    if entry_type == "run_start":
        trace_path = value.get("trace_path")
        if trace_path is not None and not isinstance(trace_path, str):
            raise SessionFormatError("trace_path must be a string or null")
        return RunStartEntry(
            entry_id=entry_id,
            parent_id=parent_id,
            created_at=created_at,
            run_id=run_id,
            runtime_fingerprint=fingerprint_from_dict(
                _require_mapping(value.get("runtime_fingerprint"), "runtime_fingerprint")
            ),
            trace_path=trace_path,
        )
    if entry_type == "message":
        message = message_from_dict(_require_mapping(value.get("message"), "message"))
        if isinstance(message, SystemMessage):
            raise SessionFormatError("SystemMessage cannot be stored as a Session entry")
        return MessageEntry(
            entry_id=entry_id,
            parent_id=parent_id,
            created_at=created_at,
            run_id=run_id,
            message=message,
        )
    if entry_type == "run_end":
        reason = value.get("reason")
        if reason not in {
            "completed",
            "terminated",
            "aborted",
            "error",
            "turn_limit",
            "interrupted",
        }:
            raise SessionFormatError("invalid Session Run end reason")
        error = value.get("error")
        if error is not None and not isinstance(error, str):
            raise SessionFormatError("run_end error must be a string or null")
        raw_error_info = value.get("error_info")
        error_info = (
            model_error_info_from_dict(
                _require_mapping(raw_error_info, "error_info")
            )
            if raw_error_info is not None
            else None
        )
        recovered = value.get("recovered", False)
        if not isinstance(recovered, bool):
            raise SessionFormatError("run_end recovered must be a boolean")
        return RunEndEntry(
            entry_id=entry_id,
            parent_id=parent_id,
            created_at=created_at,
            run_id=run_id,
            reason=cast(SessionRunEndReason, reason),
            error=error,
            error_info=error_info,
            recovered=recovered,
        )
    if entry_type == "checkpoint":
        return CheckpointEntry(
            entry_id=entry_id,
            parent_id=parent_id,
            created_at=created_at,
            run_id=run_id,
            checkpoint_id=_require_id(value.get("checkpoint_id"), "checkpoint_id"),
            path=_require_string(value.get("path"), "path"),
            sha256=_require_sha256(value.get("sha256")),
            active_entry_id=_require_id(
                value.get("active_entry_id"), "active_entry_id"
            ),
        )
    raise SessionFormatError(f"unsupported Session entry type: {entry_type!r}")


def message_to_dict(message: Message) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        raise SessionSerializationError(
            "SystemMessage is rebuilt by the current Harness and is not persisted"
        )
    value: dict[str, Any] = {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "metadata": json_value(message.metadata),
    }
    if isinstance(message, AssistantMessage):
        value["tool_calls"] = [
            {
                "id": call.id,
                "name": call.name,
                "arguments": json_value(call.arguments),
            }
            for call in message.tool_calls
        ]
        value["stop_reason"] = message.stop_reason
    elif isinstance(message, ToolResultMessage):
        value.update(
            {
                "tool_call_id": message.tool_call_id,
                "tool_name": message.tool_name,
                "is_error": message.is_error,
                "terminate": message.terminate,
            }
        )
    return value


def message_from_dict(value: Mapping[str, Any]) -> Message:
    role = value.get("role")
    message_id = _require_id(value.get("id"), "message.id")
    content = _require_string(value.get("content"), "message.content")
    created_at = _require_datetime(value.get("created_at"), "message.created_at")
    metadata = dict(
        _require_mapping(value.get("metadata", {}), "message.metadata")
    )
    if role == "system":
        return SystemMessage(
            id=message_id,
            content=content,
            created_at=created_at,
            metadata=metadata,
        )
    if role == "user":
        return UserMessage(
            id=message_id,
            content=content,
            created_at=created_at,
            metadata=metadata,
        )
    if role == "assistant":
        raw_calls = value.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise SessionFormatError("assistant tool_calls must be an array")
        calls: list[ToolCall] = []
        for raw_call in raw_calls:
            item = _require_mapping(raw_call, "tool_call")
            arguments = _require_mapping(item.get("arguments", {}), "tool_call.arguments")
            calls.append(
                ToolCall(
                    id=_require_id(item.get("id"), "tool_call.id"),
                    name=_require_string(item.get("name"), "tool_call.name"),
                    arguments=dict(arguments),
                )
            )
        stop_reason = value.get("stop_reason")
        if stop_reason not in {None, "stop", "length", "tool_use", "error", "aborted"}:
            raise SessionFormatError("invalid assistant stop_reason")
        return AssistantMessage(
            id=message_id,
            content=content,
            created_at=created_at,
            metadata=metadata,
            tool_calls=calls,
            stop_reason=cast(Any, stop_reason),
        )
    if role == "tool_result":
        is_error = value.get("is_error", False)
        terminate = value.get("terminate", False)
        if not isinstance(is_error, bool) or not isinstance(terminate, bool):
            raise SessionFormatError(
                "tool_result is_error and terminate must be booleans"
            )
        return ToolResultMessage(
            id=message_id,
            content=content,
            created_at=created_at,
            metadata=metadata,
            tool_call_id=_require_id(
                value.get("tool_call_id"), "tool_result.tool_call_id"
            ),
            tool_name=_require_string(
                value.get("tool_name"), "tool_result.tool_name"
            ),
            is_error=is_error,
            terminate=terminate,
        )
    raise SessionFormatError(f"unsupported message role: {role!r}")


def fingerprint_to_dict(value: RuntimeFingerprint) -> dict[str, str]:
    return {
        "harness": value.harness,
        "model": value.model,
        "system_prompt_sha256": value.system_prompt_sha256,
        "tools_sha256": value.tools_sha256,
        "policies_sha256": value.policies_sha256,
    }


def fingerprint_from_dict(value: Mapping[str, Any]) -> RuntimeFingerprint:
    return RuntimeFingerprint(
        harness=_require_string(value.get("harness"), "fingerprint.harness"),
        model=_require_string(value.get("model"), "fingerprint.model"),
        system_prompt_sha256=_require_sha256(value.get("system_prompt_sha256")),
        tools_sha256=_require_sha256(value.get("tools_sha256")),
        policies_sha256=_require_sha256(value.get("policies_sha256")),
    )


def model_error_info_to_dict(value: ModelErrorInfo) -> dict[str, Any]:
    return cast(dict[str, Any], json_value(asdict(value)))


def model_error_info_from_dict(value: Mapping[str, Any]) -> ModelErrorInfo:
    kind = value.get("kind")
    if kind not in {
        "authentication",
        "permission",
        "invalid_request",
        "not_found",
        "context_overflow",
        "quota_exhausted",
        "rate_limited",
        "overloaded",
        "timeout",
        "connection",
        "server",
        "protocol",
        "unknown",
    }:
        raise SessionFormatError("invalid ModelErrorInfo kind")
    retryable = value.get("retryable")
    if not isinstance(retryable, bool):
        raise SessionFormatError("ModelErrorInfo retryable must be a boolean")
    return ModelErrorInfo(
        kind=cast(ModelErrorKind, kind),
        message=_require_string(value.get("message"), "error_info.message"),
        provider=_require_string(value.get("provider"), "error_info.provider"),
        retryable=retryable,
        status_code=_optional_int(value.get("status_code"), "error_info.status_code"),
        code=_optional_string(value.get("code"), "error_info.code"),
        retry_after=_optional_number(
            value.get("retry_after"), "error_info.retry_after"
        ),
        request_id=_optional_string(
            value.get("request_id"), "error_info.request_id"
        ),
        metadata=dict(
            _require_mapping(value.get("metadata", {}), "error_info.metadata")
        ),
    )


def json_value(value: Any, *, path: str = "metadata") -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return json_value(value.value, path=path)
    if is_dataclass(value):
        return json_value(asdict(cast(Any, value)), path=path)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SessionSerializationError(
                    f"{path} contains a non-string mapping key"
                )
            result[key] = json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise SessionSerializationError(
        f"{path} contains unsupported value {type(value).__name__}"
    )


def _require_version(value: Mapping[str, Any]) -> None:
    version = value.get("schema_version")
    if version != SESSION_SCHEMA_VERSION:
        raise SessionFormatError(
            f"unsupported Session schema_version {version!r}; "
            f"expected {SESSION_SCHEMA_VERSION}"
        )


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SessionFormatError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise SessionFormatError(f"{field_name} keys must be strings")
    return cast(Mapping[str, Any], value)


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise SessionFormatError(f"{field_name} must be a string")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field_name)


def _require_datetime(value: Any, field_name: str) -> datetime:
    raw = _require_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SessionFormatError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise SessionFormatError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _require_id(value: Any, field_name: str) -> str:
    raw = _require_string(value, field_name)
    try:
        parsed = UUID(raw)
    except ValueError as exc:
        raise SessionFormatError(f"{field_name} must be a UUID") from exc
    return parsed.hex


def _optional_id(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_id(value, field_name)


def _require_sha256(value: Any) -> str:
    raw = _require_string(value, "sha256")
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw.lower()):
        raise SessionFormatError("sha256 fields must contain 64 hexadecimal characters")
    return raw.lower()


def _optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise SessionFormatError(f"{field_name} must be an integer or null")
    return value


def _optional_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SessionFormatError(f"{field_name} must be a number or null")
    return float(value)


__all__ = [
    "CheckpointEntry",
    "MessageEntry",
    "RunEndEntry",
    "RunStartEntry",
    "RuntimeFingerprint",
    "SESSION_SCHEMA_VERSION",
    "SessionEntry",
    "SessionEntryType",
    "SessionHeader",
    "SessionRunEndReason",
    "entry_from_dict",
    "entry_to_dict",
    "fingerprint_from_dict",
    "fingerprint_to_dict",
    "header_from_dict",
    "header_to_dict",
    "json_value",
    "message_from_dict",
    "message_to_dict",
    "model_error_info_from_dict",
    "model_error_info_to_dict",
    "new_id",
    "utc_now",
]
