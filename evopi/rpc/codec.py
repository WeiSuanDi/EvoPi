"""Strict JSON codecs for the RPC v1 wire envelopes.

The codec is deliberately exact. It rejects duplicate JSON keys, NaN and
Infinity, unknown schema versions, unknown or missing envelope keys,
non-object params/data, booleans used as integers, malformed or non-UTC
timestamps, invalid UUIDs, and multi-object or trailing input. Event data
conversion accepts a fixed JSON-safe value set and never falls back to
``repr``. Wire output is one compact UTF-8 JSON object per line.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from evopi.core.types import JsonObject

from .errors import RpcCodecError, RpcEventDataError
from .protocol import RpcEnvelope, RpcErrorInfo, RpcEvent, RpcRequest, RpcResponse

SCHEMA_VERSION = 1

_REQUEST_KEYS = frozenset({"request_id", "method", "params", "schema_version"})
_RESPONSE_KEYS = frozenset({"request_id", "ok", "result", "error", "schema_version"})
_EVENT_KEYS = frozenset(
    {"event_id", "sequence", "type", "data", "run_id", "created_at", "schema_version"}
)
_RESPONSE_REQUIRED = frozenset({"request_id", "ok", "schema_version"})
_ERROR_INFO_KEYS = frozenset({"code", "message", "details"})

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key: {key}")
        seen.add(key)
    return dict(pairs)


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-JSON constant: {value}")


_STRICT_DECODER = json.JSONDecoder(
    object_pairs_hook=_reject_duplicate_keys,
    parse_constant=_reject_constant,
)


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_bool(value: Any) -> bool:
    return type(value) is bool


def _check_str(obj: JsonObject, key: str) -> str:
    value = obj[key]
    if not isinstance(value, str):
        raise RpcCodecError(f"{key} must be a string")
    return value


def _check_int(obj: JsonObject, key: str) -> int:
    value = obj[key]
    if not _is_int(value):
        raise RpcCodecError(f"{key} must be an integer")
    return value


def _check_bool(obj: JsonObject, key: str) -> bool:
    value = obj[key]
    if not _is_bool(value):
        raise RpcCodecError(f"{key} must be a boolean")
    return value


def _check_dict(obj: JsonObject, key: str) -> JsonObject:
    value = obj[key]
    if not isinstance(value, dict):
        raise RpcCodecError(f"{key} must be an object")
    return value


def _check_version(obj: JsonObject) -> int:
    version = _check_int(obj, "schema_version")
    if version != SCHEMA_VERSION:
        raise RpcCodecError("unknown schema version")
    return version


def parse_utc_timestamp(value: Any) -> datetime:
    """Parse a strict canonical ISO-8601 UTC timestamp."""
    if not isinstance(value, str) or not _ISO_TIMESTAMP_RE.fullmatch(value):
        raise RpcCodecError("malformed timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RpcCodecError("malformed timestamp") from None
    if parsed.utcoffset() != timedelta(0):
        raise RpcCodecError("non-UTC timestamp")
    return parsed


def _parse_line(line: str) -> JsonObject:
    if not isinstance(line, str):
        raise RpcCodecError("line must be text")
    text = line.strip()
    if not text:
        raise RpcCodecError("empty line")
    try:
        value, end = _STRICT_DECODER.raw_decode(text)
    except ValueError as exc:
        raise RpcCodecError("invalid JSON") from exc
    if text[end:].strip():
        raise RpcCodecError("trailing content after JSON object")
    if not isinstance(value, dict):
        raise RpcCodecError("top-level value must be a JSON object")
    return value


def decode_envelope(line: str) -> RpcEnvelope:
    """Decode one wire line, discriminating the envelope by its exact key set."""
    obj = _parse_line(line)
    keys = frozenset(obj)
    if keys == _REQUEST_KEYS:
        return _decode_request_object(obj)
    if keys <= _RESPONSE_KEYS and _RESPONSE_REQUIRED <= keys:
        return _decode_response_object(obj)
    if keys == _EVENT_KEYS:
        return _decode_event_object(obj)
    raise RpcCodecError("unrecognized or malformed envelope")


def decode_request(line: str) -> RpcRequest:
    envelope = decode_envelope(line)
    if not isinstance(envelope, RpcRequest):
        raise RpcCodecError("line is not a request envelope")
    return envelope


def decode_response(line: str) -> RpcResponse:
    envelope = decode_envelope(line)
    if not isinstance(envelope, RpcResponse):
        raise RpcCodecError("line is not a response envelope")
    return envelope


def decode_event(line: str) -> RpcEvent:
    envelope = decode_envelope(line)
    if not isinstance(envelope, RpcEvent):
        raise RpcCodecError("line is not an event envelope")
    return envelope


def _decode_request_object(obj: JsonObject) -> RpcRequest:
    return RpcRequest(
        request_id=_check_str(obj, "request_id"),
        method=_check_str(obj, "method"),
        params=_check_dict(obj, "params"),
        schema_version=_check_version(obj),
    )


def _decode_response_object(obj: JsonObject) -> RpcResponse:
    result = obj.get("result")
    if result is not None and not isinstance(result, dict):
        raise RpcCodecError("result must be an object or null")
    error = _decode_error_info(obj["error"]) if obj.get("error") is not None else None
    return RpcResponse(
        request_id=_check_str(obj, "request_id"),
        ok=_check_bool(obj, "ok"),
        result=result,
        error=error,
        schema_version=_check_version(obj),
    )


def _decode_error_info(value: Any) -> RpcErrorInfo:
    if not isinstance(value, dict) or frozenset(value) != _ERROR_INFO_KEYS:
        raise RpcCodecError("malformed error info")
    code = value["code"]
    message = value["message"]
    details = value["details"]
    if not isinstance(code, str) or not isinstance(message, str) or not isinstance(details, dict):
        raise RpcCodecError("malformed error info")
    return RpcErrorInfo(code=code, message=message, details=details)


def _decode_event_object(obj: JsonObject) -> RpcEvent:
    event_id = _check_str(obj, "event_id")
    if not _UUID_RE.fullmatch(event_id):
        raise RpcCodecError("invalid event id")
    run_id = obj["run_id"]
    if run_id is not None and not isinstance(run_id, str):
        raise RpcCodecError("run_id must be a string or null")
    return RpcEvent(
        event_id=event_id,
        sequence=_check_int(obj, "sequence"),
        type=_check_str(obj, "type"),
        data=_check_dict(obj, "data"),
        run_id=run_id,
        created_at=parse_utc_timestamp(obj["created_at"]),
        schema_version=_check_version(obj),
    )


def _encode(payload: JsonObject) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RpcCodecError("envelope is not JSON-safe") from exc


def encode_request(request: RpcRequest) -> str:
    return _encode(
        {
            "request_id": request.request_id,
            "method": request.method,
            "params": request.params,
            "schema_version": request.schema_version,
        }
    )


def encode_response(response: RpcResponse) -> str:
    payload: JsonObject = {
        "request_id": response.request_id,
        "ok": response.ok,
        "schema_version": response.schema_version,
    }
    if response.result is not None:
        payload["result"] = response.result
    if response.error is not None:
        payload["error"] = {
            "code": response.error.code,
            "message": response.error.message,
            "details": response.error.details,
        }
    return _encode(payload)


def encode_event(event: RpcEvent) -> str:
    return _encode(
        {
            "event_id": event.event_id,
            "sequence": event.sequence,
            "type": event.type,
            "data": event.data,
            "run_id": event.run_id,
            "created_at": event.created_at.isoformat(),
            "schema_version": event.schema_version,
        }
    )


def extract_request_id(line: str) -> str | None:
    """Return the ``request_id`` of a line that failed envelope validation.

    The connection uses this to answer protocol-invalid requests with an
    ``invalid_request`` response whenever a request id is present; lines that
    cannot be parsed at all force a clean connection failure instead.
    """
    text = line.strip()
    try:
        value, end = _STRICT_DECODER.raw_decode(text)
    except ValueError:
        return None
    if text[end:].strip() or not isinstance(value, dict):
        return None
    request_id = value.get("request_id")
    return request_id if isinstance(request_id, str) else None


def to_event_data(value: Any) -> Any:
    """Strictly convert a value to JSON-safe event data.

    Accepts JSON primitives, mappings (string keys only), sequences,
    dataclasses, enums, ``Path``, and ``datetime``/``date``. Any other value
    raises ``RpcEventDataError`` without serializing or echoing the value.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RpcEventDataError("non-finite float in event data")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return to_event_data(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field_.name: to_event_data(getattr(value, field_.name)) for field_ in fields(value)
        }
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RpcEventDataError("non-string key in event data mapping")
            result[key] = to_event_data(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_event_data(item) for item in value]
    raise RpcEventDataError("unsupported value in event data")


__all__ = [
    "SCHEMA_VERSION",
    "decode_envelope",
    "decode_event",
    "decode_request",
    "decode_response",
    "encode_event",
    "encode_request",
    "encode_response",
    "extract_request_id",
    "parse_utc_timestamp",
    "to_event_data",
]
