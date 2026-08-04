"""Strict schema-v1 codecs for the Confirmation v2 persistent protocol.

Every codec validates exact keys, exact field types, UTC timestamps, JSON-safe
nested values, strict ToolCall reconstruction, and the schema version. Unknown
versions, unknown fields, and unsupported values fail closed with
:class:`ConfirmationFormatError`; there is never a ``repr`` fallback.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, cast, get_args

from evopi.core.tool import ToolArgumentError, ToolCall
from evopi.core.types import JsonObject
from evopi.harness.confirmation import (
    ConfirmationDecision,
    ConfirmationFormatError,
    ConfirmationRecord,
    ConfirmationRequest,
    ConfirmationResponse,
    ConfirmationStatus,
    ConfirmationTransition,
)
from evopi.policy.types import HookName, RiskLevel

SCHEMA_VERSION = 1

_KNOWN_HOOKS = get_args(HookName)
_KNOWN_RISK_LEVELS = get_args(RiskLevel)
_KNOWN_STATUSES = get_args(ConfirmationStatus)
_KNOWN_DECISIONS = get_args(ConfirmationDecision)

_MAX_ID_LENGTH = 512

_DECISION_FOR_STATUS: dict[str, str] = {
    "approved": "approve",
    "denied": "deny",
    "cancelled": "cancelled",
}

_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "hook",
        "reason",
        "risk_level",
        "policy_names",
        "tool_call",
        "arguments",
        "created_at",
        "metadata",
        "run_id",
        "session_id",
        "expires_at",
    }
)
_RESPONSE_KEYS = frozenset(
    {"schema_version", "request_id", "decision", "reason", "metadata"}
)
_RECORD_KEYS = frozenset(
    {"schema_version", "request", "status", "runtime_id", "revision", "response", "updated_at"}
)
_TRANSITION_KEYS = frozenset(
    {"schema_version", "request_id", "expected_revision", "status", "response"}
)
_TOOL_CALL_KEYS = frozenset({"id", "name", "arguments", "argument_error"})
_ARGUMENT_ERROR_KEYS = frozenset({"code", "message", "raw_fragment"})


def _check_version(data: JsonObject, *, field: str) -> None:
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfirmationFormatError(
            f"field '{field}.schema_version' must be an integer",
            details={"field": f"{field}.schema_version"},
        )
    if version != SCHEMA_VERSION:
        raise ConfirmationFormatError(
            f"unsupported schema version {version!r} in '{field}'",
            details={"field": field, "schema_version": version},
        )


def _check_keys(data: JsonObject, allowed: frozenset[str], *, field: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfirmationFormatError(
            f"unknown field(s) in '{field}': {', '.join(unknown)}",
            details={"field": field, "unknown": unknown},
        )
    missing = sorted(allowed - set(data))
    if missing:
        raise ConfirmationFormatError(
            f"missing field(s) in '{field}': {', '.join(missing)}",
            details={"field": field, "missing": missing},
        )


def _check_str(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ConfirmationFormatError(
            f"field '{field}' must be a string", details={"field": field}
        )
    return value


def _check_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_ID_LENGTH:
        raise ConfirmationFormatError(
            f"field '{field}' must be a non-empty string id of at most "
            f"{_MAX_ID_LENGTH} characters",
            details={"field": field},
        )
    return value


def _check_int(value: Any, *, field: str) -> int:
    # Booleans are ints in Python but are never valid integer fields.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfirmationFormatError(
            f"field '{field}' must be an integer", details={"field": field}
        )
    return value


def _check_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfirmationFormatError(
            f"field '{field}' must be a boolean", details={"field": field}
        )
    return value


def _check_literal(value: Any, *, field: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ConfirmationFormatError(
            f"field '{field}' has an unsupported value {value!r}",
            details={"field": field, "value": value},
        )
    return value


def _check_utc_datetime(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ConfirmationFormatError(
            f"field '{field}' must be an ISO-8601 datetime string",
            details={"field": field},
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfirmationFormatError(
            f"field '{field}' is not a valid ISO-8601 datetime",
            details={"field": field},
        ) from exc
    offset = parsed.utcoffset()
    if offset is None:
        raise ConfirmationFormatError(
            f"field '{field}' must carry a timezone offset",
            details={"field": field},
        )
    if offset != timedelta(0):
        raise ConfirmationFormatError(
            f"field '{field}' must use UTC (offset zero)",
            details={"field": field, "offset": offset.total_seconds()},
        )
    return parsed


def _check_str_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfirmationFormatError(
            f"field '{field}' must be an array of strings", details={"field": field}
        )
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfirmationFormatError(
                f"field '{field}[{index}]' must be a string",
                details={"field": field},
            )
        result.append(item)
    return tuple(result)


def _check_json_value(value: Any, *, field: str) -> None:
    """Reject any value that is not representable as JSON (no repr fallback)."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfirmationFormatError(
                f"field '{field}' contains a non-finite float",
                details={"field": field, "type": "float"},
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfirmationFormatError(
                    f"field '{field}' contains a non-string key of type "
                    f"{type(key).__name__}",
                    details={"field": field, "type": type(key).__name__},
                )
            _check_json_value(item, field=field)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_value(item, field=f"{field}[{index}]")
        return
    raise ConfirmationFormatError(
        f"field '{field}' contains a non-JSON value of type {type(value).__name__}",
        details={"field": field, "type": type(value).__name__},
    )


def _check_json_object(value: Any, *, field: str) -> JsonObject:
    _check_json_value(value, field=field)
    if not isinstance(value, dict):
        raise ConfirmationFormatError(
            f"field '{field}' must be an object", details={"field": field}
        )
    return value


def _validate_status_response_pair(
    status: str,
    response: ConfirmationResponse | None,
    *,
    request_id: str,
) -> None:
    """Enforce the frozen terminal-state and response semantics (Finding A)."""
    if status == "pending":
        if response is not None:
            raise ConfirmationFormatError(
                "pending records must not carry a response",
                details={"request_id": request_id},
            )
        return
    if status == "orphaned":
        if response is not None:
            raise ConfirmationFormatError(
                "orphaned records must not carry a response",
                details={"request_id": request_id},
            )
        return
    if response is None:
        raise ConfirmationFormatError(
            f"status {status!r} requires a correlated response",
            details={"request_id": request_id, "status": status},
        )
    if response.request_id != request_id:
        raise ConfirmationFormatError(
            f"response {response.request_id!r} does not correlate to "
            f"request {request_id!r}",
            details={"request_id": request_id, "response_request_id": response.request_id},
        )
    expected = _DECISION_FOR_STATUS.get(status)
    if expected is not None:
        if response.decision != expected:
            raise ConfirmationFormatError(
                f"status {status!r} requires decision {expected!r}",
                details={"request_id": request_id, "decision": response.decision},
            )
        return
    if status == "expired":
        if response.decision != "deny":
            raise ConfirmationFormatError(
                "expired requires a deny decision",
                details={"request_id": request_id},
            )
        if response.metadata.get("automatic") is not True:
            raise ConfirmationFormatError(
                "expired requires metadata.automatic=true",
                details={"request_id": request_id},
            )
        if response.metadata.get("expired") is not True:
            raise ConfirmationFormatError(
                "expired requires metadata.expired=true",
                details={"request_id": request_id},
            )
        return
    raise ConfirmationFormatError(
        f"unsupported status {status!r}", details={"status": status}
    )


def validate_record_invariants(record: ConfirmationRecord) -> None:
    """Reject records the decoder would refuse to reconstruct (Findings A/F)."""
    if isinstance(record.revision, bool) or record.revision < 1:
        raise ConfirmationFormatError(
            "record revision must be positive",
            details={"revision": record.revision},
        )
    _validate_status_response_pair(
        record.status, record.response, request_id=record.request.id
    )


def validate_transition_invariants(transition: ConfirmationTransition) -> None:
    """Reject transitions the decoder would refuse to reconstruct (Findings A/F)."""
    if (
        isinstance(transition.expected_revision, bool)
        or transition.expected_revision < 1
    ):
        raise ConfirmationFormatError(
            "transition expected_revision must be positive",
            details={"expected_revision": transition.expected_revision},
        )
    _validate_status_response_pair(
        transition.status, transition.response, request_id=transition.request_id
    )


def _encode_datetime(value: datetime, *, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfirmationFormatError(
            f"field '{field}' must carry a timezone offset", details={"field": field}
        )
    return value.isoformat()


def _encode_optional_datetime(value: datetime | None, *, field: str) -> str | None:
    if value is None:
        return None
    return _encode_datetime(value, field=field)


def _encode_tool_call(call: ToolCall | None) -> JsonObject | None:
    if call is None:
        return None
    argument_error = call.argument_error
    return {
        "id": _check_id(call.id, field="tool_call.id"),
        "name": _check_str(call.name, field="tool_call.name"),
        "arguments": _check_json_object(call.arguments, field="tool_call.arguments"),
        "argument_error": None
        if argument_error is None
        else {
            "code": _check_str(argument_error.code, field="tool_call.argument_error.code"),
            "message": _check_str(
                argument_error.message, field="tool_call.argument_error.message"
            ),
            "raw_fragment": _encode_optional_str(
                argument_error.raw_fragment,
                field="tool_call.argument_error.raw_fragment",
            ),
        },
    }


def _decode_tool_call(value: Any) -> ToolCall | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfirmationFormatError(
            "field 'tool_call' must be an object or null", details={"field": "tool_call"}
        )
    _check_keys(value, _TOOL_CALL_KEYS, field="tool_call")
    argument_error = value["argument_error"]
    if argument_error is not None:
        if not isinstance(argument_error, dict):
            raise ConfirmationFormatError(
                "field 'tool_call.argument_error' must be an object or null",
                details={"field": "tool_call.argument_error"},
            )
        _check_keys(argument_error, _ARGUMENT_ERROR_KEYS, field="tool_call.argument_error")
        argument_error = ToolArgumentError(
            code=_check_str(argument_error["code"], field="tool_call.argument_error.code"),
            message=_check_str(
                argument_error["message"], field="tool_call.argument_error.message"
            ),
            raw_fragment=_decode_optional_str(
                argument_error["raw_fragment"], field="tool_call.argument_error.raw_fragment"
            ),
        )
    return ToolCall(
        id=_check_id(value["id"], field="tool_call.id"),
        name=_check_str(value["name"], field="tool_call.name"),
        arguments=_check_json_object(value["arguments"], field="tool_call.arguments"),
        argument_error=argument_error,
    )


def _encode_optional_str(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    return _check_str(value, field=field)


def _decode_optional_str(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _check_str(value, field=field)


def _decode_optional_datetime(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    return _check_utc_datetime(value, field=field)


def encode_request(request: ConfirmationRequest) -> JsonObject:
    """Encode a request with exact keys and strict types (schema version 1)."""
    _check_literal(request.hook, field="hook", allowed=_KNOWN_HOOKS)
    _check_literal(request.risk_level, field="risk_level", allowed=_KNOWN_RISK_LEVELS)
    metadata = _check_json_object(request.metadata, field="metadata")
    arguments = (
        None
        if request.arguments is None
        else _check_json_object(request.arguments, field="arguments")
    )
    policy_names = [_check_str(policy, field="policy_names") for policy in request.policy_names]
    return {
        "schema_version": SCHEMA_VERSION,
        "id": _check_id(request.id, field="id"),
        "hook": request.hook,
        "reason": _check_str(request.reason, field="reason"),
        "risk_level": request.risk_level,
        "policy_names": policy_names,
        "tool_call": _encode_tool_call(request.tool_call),
        "arguments": arguments,
        "created_at": _encode_datetime(request.created_at, field="created_at"),
        "metadata": metadata,
        "run_id": _encode_optional_str(request.run_id, field="run_id"),
        "session_id": _encode_optional_str(request.session_id, field="session_id"),
        "expires_at": _encode_optional_datetime(request.expires_at, field="expires_at"),
    }


def decode_request(data: JsonObject) -> ConfirmationRequest:
    """Decode a request payload strictly, failing closed on any deviation."""
    if not isinstance(data, dict):
        raise ConfirmationFormatError("request payload must be an object")
    _check_keys(data, _REQUEST_KEYS, field="request")
    _check_version(data, field="request")
    policy_names = _check_str_tuple(data["policy_names"], field="policy_names")
    return ConfirmationRequest(
        id=_check_id(data["id"], field="id"),
        hook=cast(HookName, _check_literal(data["hook"], field="hook", allowed=_KNOWN_HOOKS)),
        reason=_check_str(data["reason"], field="reason"),
        risk_level=cast(
            RiskLevel,
            _check_literal(data["risk_level"], field="risk_level", allowed=_KNOWN_RISK_LEVELS),
        ),
        policy_names=policy_names,
        tool_call=_decode_tool_call(data["tool_call"]),
        arguments=(
            None if data["arguments"] is None else _check_json_object(data["arguments"], field="arguments")
        ),
        created_at=_check_utc_datetime(data["created_at"], field="created_at"),
        metadata=_check_json_object(data["metadata"], field="metadata"),
        run_id=_decode_optional_str(data["run_id"], field="run_id"),
        session_id=_decode_optional_str(data["session_id"], field="session_id"),
        expires_at=_decode_optional_datetime(data["expires_at"], field="expires_at"),
    )


def encode_response(response: ConfirmationResponse) -> JsonObject:
    """Encode a response with exact keys and strict types (schema version 1)."""
    _check_literal(response.decision, field="decision", allowed=_KNOWN_DECISIONS)
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": _check_id(response.request_id, field="request_id"),
        "decision": response.decision,
        "reason": _check_str(response.reason, field="reason"),
        "metadata": _check_json_object(response.metadata, field="metadata"),
    }


def decode_response(data: JsonObject) -> ConfirmationResponse:
    """Decode a response payload strictly, failing closed on any deviation."""
    if not isinstance(data, dict):
        raise ConfirmationFormatError("response payload must be an object")
    _check_keys(data, _RESPONSE_KEYS, field="response")
    _check_version(data, field="response")
    return ConfirmationResponse(
        request_id=_check_id(data["request_id"], field="request_id"),
        decision=cast(
            ConfirmationDecision,
            _check_literal(data["decision"], field="decision", allowed=_KNOWN_DECISIONS),
        ),
        reason=_check_str(data["reason"], field="reason"),
        metadata=_check_json_object(data["metadata"], field="metadata"),
    )


def encode_record(record: ConfirmationRecord) -> JsonObject:
    """Encode a record snapshot with exact keys and strict types (schema version 1)."""
    _check_literal(record.status, field="status", allowed=_KNOWN_STATUSES)
    _check_id(record.runtime_id, field="runtime_id")
    _check_int(record.revision, field="revision")
    validate_record_invariants(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "request": encode_request(record.request),
        "status": record.status,
        "runtime_id": record.runtime_id,
        "revision": record.revision,
        "response": None if record.response is None else encode_response(record.response),
        "updated_at": _encode_datetime(record.updated_at, field="updated_at"),
    }


def decode_record(data: JsonObject) -> ConfirmationRecord:
    """Decode a record payload strictly, failing closed on any deviation."""
    if not isinstance(data, dict):
        raise ConfirmationFormatError("record payload must be an object")
    _check_keys(data, _RECORD_KEYS, field="record")
    _check_version(data, field="record")
    record = ConfirmationRecord(
        request=decode_request(data["request"]),
        status=cast(
            ConfirmationStatus,
            _check_literal(data["status"], field="status", allowed=_KNOWN_STATUSES),
        ),
        runtime_id=_check_id(data["runtime_id"], field="runtime_id"),
        revision=_check_int(data["revision"], field="revision"),
        response=None if data["response"] is None else decode_response(data["response"]),
        updated_at=_check_utc_datetime(data["updated_at"], field="updated_at"),
    )
    validate_record_invariants(record)
    return record


def encode_transition(transition: ConfirmationTransition) -> JsonObject:
    """Encode a transition with exact keys and strict types (schema version 1)."""
    _check_literal(transition.status, field="status", allowed=_KNOWN_STATUSES)
    _check_int(transition.expected_revision, field="expected_revision")
    validate_transition_invariants(transition)
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": _check_id(transition.request_id, field="request_id"),
        "expected_revision": transition.expected_revision,
        "status": transition.status,
        "response": None if transition.response is None else encode_response(transition.response),
    }


def decode_transition(data: JsonObject) -> ConfirmationTransition:
    """Decode a transition payload strictly, failing closed on any deviation."""
    if not isinstance(data, dict):
        raise ConfirmationFormatError("transition payload must be an object")
    _check_keys(data, _TRANSITION_KEYS, field="transition")
    _check_version(data, field="transition")
    transition = ConfirmationTransition(
        request_id=_check_id(data["request_id"], field="request_id"),
        expected_revision=_check_int(data["expected_revision"], field="expected_revision"),
        status=cast(
            ConfirmationStatus,
            _check_literal(data["status"], field="status", allowed=_KNOWN_STATUSES),
        ),
        response=None if data["response"] is None else decode_response(data["response"]),
    )
    validate_transition_invariants(transition)
    return transition


__all__ = [
    "SCHEMA_VERSION",
    "decode_record",
    "decode_request",
    "decode_response",
    "decode_transition",
    "encode_record",
    "encode_request",
    "encode_response",
    "encode_transition",
    "validate_record_invariants",
    "validate_transition_invariants",
]
