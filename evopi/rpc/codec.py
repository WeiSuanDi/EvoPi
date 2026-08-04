"""Strict JSON codecs for the RPC v1 wire envelopes.

The codec is deliberately exact, and validation is symmetric: the same rules
apply on encode and decode, so the encoder never emits a payload the decoder
rejects. It rejects duplicate JSON keys, NaN and Infinity, unknown schema
versions, unknown or missing envelope keys, non-object params/data/result,
booleans used as integers, malformed or non-UTC timestamps, invalid UUIDs,
empty request IDs/method names/error codes/event types, sequence numbers
below 1, and multi-object or trailing input.

The response envelope is canonical: all five keys are always present.
``ok=true`` requires an object ``result`` and ``error=null``; ``ok=false``
requires ``result=null`` and exactly one error object. Event data conversion
accepts a fixed JSON-safe value set and never falls back to ``repr``. Wire
output is one compact UTF-8 JSON object per line.
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


def _require_nonempty_str(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise RpcCodecError("expected a non-empty string")
    return value


def _is_utc_datetime(value: Any) -> bool:
    return isinstance(value, datetime) and value.utcoffset() == timedelta(0)


def _check_nonempty_str(obj: JsonObject, key: str) -> str:
    return _require_nonempty_str(obj[key])


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
    return _check_version_value(_check_int(obj, "schema_version"))


def _check_version_value(version: Any) -> int:
    if not _is_int(version) or version != SCHEMA_VERSION:
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


# ---------------------------------------------------------------------------
# Encoding: validate the dataclass instance, build the canonical payload, and
# only then emit the wire line. A crafted invalid instance fails before output.
# ---------------------------------------------------------------------------


def _require_dict(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise RpcCodecError("expected an object")
    return value


def _request_payload(request: RpcRequest) -> JsonObject:
    return {
        "request_id": _require_nonempty_str(request.request_id),
        "method": _require_nonempty_str(request.method),
        "params": _require_dict(request.params),
        "schema_version": _check_version_value(request.schema_version),
    }


def _error_info_payload(info: RpcErrorInfo) -> JsonObject:
    if not isinstance(info, RpcErrorInfo):
        raise RpcCodecError("error must be an error info object")
    return {
        "code": _require_nonempty_str(info.code),
        "message": _require_nonempty_str(info.message),
        "details": _require_dict(info.details),
    }


def _response_payload(response: RpcResponse) -> JsonObject:
    """Validate the canonical ok/result/error invariant and build the payload."""
    request_id = _require_nonempty_str(response.request_id)
    if not _is_bool(response.ok):
        raise RpcCodecError("response ok must be a boolean")
    schema_version = _check_version_value(response.schema_version)
    if response.ok:
        if not isinstance(response.result, dict):
            raise RpcCodecError("successful response requires an object result")
        if response.error is not None:
            raise RpcCodecError("successful response cannot carry an error")
        return {
            "request_id": request_id,
            "ok": True,
            "result": response.result,
            "error": None,
            "schema_version": schema_version,
        }
    if response.result is not None:
        raise RpcCodecError("failed response cannot carry a result")
    if not isinstance(response.error, RpcErrorInfo):
        raise RpcCodecError("failed response requires an error")
    return {
        "request_id": request_id,
        "ok": False,
        "result": None,
        "error": _error_info_payload(response.error),
        "schema_version": schema_version,
    }


def _event_payload(event: RpcEvent) -> JsonObject:
    if not isinstance(event.event_id, str) or not _UUID_RE.fullmatch(event.event_id):
        raise RpcCodecError("invalid event id")
    if not _is_int(event.sequence) or event.sequence < 1:
        raise RpcCodecError("event sequence must be a positive integer")
    if not _is_utc_datetime(event.created_at):
        raise RpcCodecError("event timestamp must be a UTC datetime")
    run_id = event.run_id
    if run_id is not None and not isinstance(run_id, str):
        raise RpcCodecError("run_id must be a string or null")
    return {
        "event_id": event.event_id,
        "sequence": event.sequence,
        "type": _require_nonempty_str(event.type),
        "data": _require_dict(event.data),
        "run_id": run_id,
        "created_at": event.created_at.isoformat(),
        "schema_version": _check_version_value(event.schema_version),
    }


def _encode(payload: JsonObject) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RpcCodecError("envelope is not JSON-safe") from exc


def encode_request(request: RpcRequest) -> str:
    return _encode(_request_payload(request))


def encode_response(response: RpcResponse) -> str:
    return _encode(_response_payload(response))


def encode_event(event: RpcEvent) -> str:
    return _encode(_event_payload(event))


# ---------------------------------------------------------------------------
# Decoding: the same rules, applied to parsed JSON objects.
# ---------------------------------------------------------------------------


def decode_envelope(line: str) -> RpcEnvelope:
    """Decode one wire line, discriminating the envelope by its exact key set."""
    obj = _parse_line(line)
    keys = frozenset(obj)
    if keys == _REQUEST_KEYS:
        return _decode_request_object(obj)
    if keys == _RESPONSE_KEYS:
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
        request_id=_check_nonempty_str(obj, "request_id"),
        method=_check_nonempty_str(obj, "method"),
        params=_check_dict(obj, "params"),
        schema_version=_check_version(obj),
    )


def _decode_response_object(obj: JsonObject) -> RpcResponse:
    request_id = _check_nonempty_str(obj, "request_id")
    ok = _check_bool(obj, "ok")
    _check_version(obj)
    result = obj["result"]
    error = obj["error"]
    if ok:
        if not isinstance(result, dict):
            raise RpcCodecError("successful response requires an object result")
        if error is not None:
            raise RpcCodecError("successful response cannot carry an error")
        return RpcResponse(request_id=request_id, ok=True, result=result)
    if result is not None:
        raise RpcCodecError("failed response cannot carry a result")
    if error is None:
        raise RpcCodecError("failed response requires an error")
    return RpcResponse(request_id=request_id, ok=False, error=_decode_error_info(error))


def _decode_error_info(value: Any) -> RpcErrorInfo:
    if not isinstance(value, dict) or frozenset(value) != _ERROR_INFO_KEYS:
        raise RpcCodecError("malformed error info")
    code = _require_nonempty_str(value["code"])
    message = _require_nonempty_str(value["message"])
    details = value["details"]
    if not isinstance(details, dict):
        raise RpcCodecError("malformed error info")
    return RpcErrorInfo(code=code, message=message, details=details)


def _decode_event_object(obj: JsonObject) -> RpcEvent:
    event_id = _check_nonempty_str(obj, "event_id")
    if not _UUID_RE.fullmatch(event_id):
        raise RpcCodecError("invalid event id")
    sequence = _check_int(obj, "sequence")
    if sequence < 1:
        raise RpcCodecError("event sequence must be a positive integer")
    run_id = obj["run_id"]
    if run_id is not None and not isinstance(run_id, str):
        raise RpcCodecError("run_id must be a string or null")
    return RpcEvent(
        event_id=event_id,
        sequence=sequence,
        type=_check_nonempty_str(obj, "type"),
        data=_check_dict(obj, "data"),
        run_id=run_id,
        created_at=parse_utc_timestamp(obj["created_at"]),
        schema_version=_check_version(obj),
    )


def extract_request_id(line: str) -> str | None:
    """Return the ``request_id`` of a request-shaped line that failed validation.

    Only request-shaped input (an object containing the request ``method``
    discriminator) may be answered with ``invalid_request``; response- and
    event-shaped lines return ``None`` so the connection closes with a
    structured protocol error instead of entering a response loop. Lines that
    cannot be parsed at all also return ``None``.
    """
    text = line.strip()
    try:
        value, end = _STRICT_DECODER.raw_decode(text)
    except ValueError:
        return None
    if text[end:].strip() or not isinstance(value, dict):
        return None
    if "method" not in value:
        return None  # response/event-shaped or unrecognized: never reply invalid_request
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    return request_id


def validate_request(request: RpcRequest) -> None:
    """Enforce the wire request invariants on a dataclass instance.

    Used by the generic server at its public dispatch boundary so crafted
    instances the codec would reject never reach the Host.
    """
    payload = _request_payload(request)
    try:
        json.dumps(payload, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RpcCodecError("request is not JSON-safe") from exc


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
    "validate_request",
]
