"""Shared strict decoding for Remote JSON objects and UTC timestamps."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any


class StrictRemoteJsonError(ValueError):
    """Raised when persisted Remote JSON is ambiguous or malformed."""


def decode_strict_json_object(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except StrictRemoteJsonError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictRemoteJsonError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise StrictRemoteJsonError("JSON root must be an object")
    return value


def parse_utc_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("UTC datetime must be a non-empty string")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("datetime must use UTC")
    return parsed


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise StrictRemoteJsonError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> Any:
    raise StrictRemoteJsonError(f"invalid JSON constant: {value}")


__all__ = [
    "StrictRemoteJsonError",
    "decode_strict_json_object",
    "parse_utc_datetime",
]
