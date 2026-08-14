"""Strict JSONL codec for RPC v2 envelopes."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from evopi.core.types import JsonObject

from .errors import RpcCodecError
from .protocol_v2 import (
    RpcV2Envelope,
    RpcV2ErrorInfo,
    RpcV2Event,
    RpcV2Request,
    RpcV2Response,
)

SCHEMA_VERSION_V2 = 2

_REQUEST_KEYS = frozenset({"request_id", "method", "params", "schema_version"})
_RESPONSE_KEYS = frozenset({"request_id", "ok", "result", "error", "schema_version"})
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "stream_id",
        "sequence",
        "type",
        "data",
        "run_id",
        "created_at",
        "schema_version",
    }
)
_ERROR_KEYS = frozenset({"code", "message", "details"})
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-JSON constant: {value}")


_DECODER = json.JSONDecoder(
    object_pairs_hook=_reject_duplicate_keys,
    parse_constant=_reject_constant,
)


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RpcCodecError(f"{field} must be a non-empty string")
    return value


def _object(value: Any, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise RpcCodecError(f"{field} must be an object")
    return value


def _version(value: Any) -> int:
    if type(value) is not int or value != SCHEMA_VERSION_V2:
        raise RpcCodecError("unknown schema version")
    return value


def _uuid(value: Any, field: str) -> str:
    text = _nonempty(value, field)
    if not _UUID_RE.fullmatch(text):
        raise RpcCodecError(f"invalid {field}")
    return text


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RpcCodecError("malformed timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RpcCodecError("malformed timestamp") from None
    if parsed.utcoffset() != timedelta(0):
        raise RpcCodecError("non-UTC timestamp")
    return parsed


def _error_payload(error: RpcV2ErrorInfo) -> JsonObject:
    return {
        "code": _nonempty(error.code, "error.code"),
        "message": _nonempty(error.message, "error.message"),
        "details": _object(error.details, "error.details"),
    }


def _encode(payload: JsonObject) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RpcCodecError("envelope is not JSON-safe") from exc


def encode_v2_request(request: RpcV2Request) -> str:
    return _encode(
        {
            "request_id": _nonempty(request.request_id, "request_id"),
            "method": _nonempty(request.method, "method"),
            "params": _object(request.params, "params"),
            "schema_version": _version(request.schema_version),
        }
    )


def encode_v2_response(response: RpcV2Response) -> str:
    request_id = _nonempty(response.request_id, "request_id")
    if type(response.ok) is not bool:
        raise RpcCodecError("response ok must be a boolean")
    if response.ok:
        if not isinstance(response.result, dict) or response.error is not None:
            raise RpcCodecError("successful response requires only an object result")
        result: JsonObject | None = response.result
        error: JsonObject | None = None
    else:
        if response.result is not None or not isinstance(response.error, RpcV2ErrorInfo):
            raise RpcCodecError("failed response requires only an error")
        result = None
        error = _error_payload(response.error)
    return _encode(
        {
            "request_id": request_id,
            "ok": response.ok,
            "result": result,
            "error": error,
            "schema_version": _version(response.schema_version),
        }
    )


def encode_v2_event(event: RpcV2Event) -> str:
    if type(event.sequence) is not int or event.sequence < 1:
        raise RpcCodecError("event sequence must be a positive integer")
    if event.created_at.utcoffset() != timedelta(0):
        raise RpcCodecError("event timestamp must be UTC")
    if event.run_id is not None and not isinstance(event.run_id, str):
        raise RpcCodecError("run_id must be a string or null")
    return _encode(
        {
            "event_id": _uuid(event.event_id, "event id"),
            "stream_id": _uuid(event.stream_id, "stream id"),
            "sequence": event.sequence,
            "type": _nonempty(event.type, "type"),
            "data": _object(event.data, "data"),
            "run_id": event.run_id,
            "created_at": event.created_at.isoformat(),
            "schema_version": _version(event.schema_version),
        }
    )


def _parse(line: str) -> JsonObject:
    if not isinstance(line, str) or not line.strip():
        raise RpcCodecError("empty line")
    text = line.strip()
    try:
        value, end = _DECODER.raw_decode(text)
    except ValueError as exc:
        raise RpcCodecError("invalid JSON") from exc
    if text[end:].strip() or not isinstance(value, dict):
        raise RpcCodecError("malformed v2 envelope")
    return value


def extract_v2_request_id(line: str) -> str | None:
    """Return a correlatable ID only for request-shaped malformed v2 input."""

    try:
        value = _parse(line)
    except RpcCodecError:
        return None
    if "method" not in value or value.get("schema_version") != SCHEMA_VERSION_V2:
        return None
    request_id = value.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else None


def decode_v2_envelope(line: str) -> RpcV2Envelope:
    obj = _parse(line)
    keys = frozenset(obj)
    if keys == _REQUEST_KEYS:
        return RpcV2Request(
            request_id=_nonempty(obj["request_id"], "request_id"),
            method=_nonempty(obj["method"], "method"),
            params=_object(obj["params"], "params"),
            schema_version=_version(obj["schema_version"]),
        )
    if keys == _RESPONSE_KEYS:
        return _decode_response(obj)
    if keys == _EVENT_KEYS:
        return _decode_event(obj)
    raise RpcCodecError("malformed v2 envelope")


def _decode_response(obj: JsonObject) -> RpcV2Response:
    request_id = _nonempty(obj["request_id"], "request_id")
    ok = obj["ok"]
    if type(ok) is not bool:
        raise RpcCodecError("response ok must be a boolean")
    _version(obj["schema_version"])
    if ok:
        if not isinstance(obj["result"], dict) or obj["error"] is not None:
            raise RpcCodecError("successful response requires only an object result")
        return RpcV2Response(request_id=request_id, ok=True, result=obj["result"])
    if obj["result"] is not None or not isinstance(obj["error"], dict):
        raise RpcCodecError("failed response requires only an error")
    raw_error = obj["error"]
    if frozenset(raw_error) != _ERROR_KEYS:
        raise RpcCodecError("malformed error info")
    return RpcV2Response(
        request_id=request_id,
        ok=False,
        error=RpcV2ErrorInfo(
            code=_nonempty(raw_error["code"], "error.code"),
            message=_nonempty(raw_error["message"], "error.message"),
            details=_object(raw_error["details"], "error.details"),
        ),
    )


def _decode_event(obj: JsonObject) -> RpcV2Event:
    sequence = obj["sequence"]
    if type(sequence) is not int or sequence < 1:
        raise RpcCodecError("event sequence must be a positive integer")
    run_id = obj["run_id"]
    if run_id is not None and not isinstance(run_id, str):
        raise RpcCodecError("run_id must be a string or null")
    return RpcV2Event(
        event_id=_uuid(obj["event_id"], "event id"),
        stream_id=_uuid(obj["stream_id"], "stream id"),
        sequence=sequence,
        type=_nonempty(obj["type"], "type"),
        data=_object(obj["data"], "data"),
        run_id=run_id,
        created_at=_timestamp(obj["created_at"]),
        schema_version=_version(obj["schema_version"]),
    )


def decode_v2_request(line: str) -> RpcV2Request:
    envelope = decode_v2_envelope(line)
    if not isinstance(envelope, RpcV2Request):
        raise RpcCodecError("line is not a v2 request envelope")
    return envelope


def decode_v2_response(line: str) -> RpcV2Response:
    envelope = decode_v2_envelope(line)
    if not isinstance(envelope, RpcV2Response):
        raise RpcCodecError("line is not a v2 response envelope")
    return envelope


def decode_v2_event(line: str) -> RpcV2Event:
    envelope = decode_v2_envelope(line)
    if not isinstance(envelope, RpcV2Event):
        raise RpcCodecError("line is not a v2 event envelope")
    return envelope


__all__ = [
    "SCHEMA_VERSION_V2",
    "decode_v2_envelope",
    "decode_v2_event",
    "decode_v2_request",
    "decode_v2_response",
    "encode_v2_event",
    "encode_v2_request",
    "encode_v2_response",
    "extract_v2_request_id",
]
