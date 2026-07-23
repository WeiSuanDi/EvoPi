"""Shared HTTP/SSE helpers for model adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from evopi.core.model_errors import (
    ModelError,
    ModelErrorInfo,
    ModelErrorKind,
    RETRYABLE_MODEL_ERROR_KINDS,
)

_MESSAGE_LIMIT = 1000
_QUOTA_MARKERS = (
    "insufficient_quota",
    "billing",
    "credit balance",
    "quota exceeded",
    "quota_exceeded",
)
_CONTEXT_MARKERS = (
    "context_length_exceeded",
    "context window",
    "maximum context length",
    "prompt is too long",
    "too many tokens",
)
_OVERLOAD_MARKERS = ("overloaded", "over_capacity", "capacity")
_AUTHENTICATION_MARKERS = ("authentication_error", "invalid_api_key", "unauthorized")
_PERMISSION_MARKERS = ("permission_error", "permission_denied", "forbidden")
_RATE_LIMIT_MARKERS = ("rate_limit", "rate_limited", "too_many_requests")
_NOT_FOUND_MARKERS = ("not_found", "model_not_found")
_TIMEOUT_MARKERS = ("timeout", "timed_out", "request_timeout")
_SERVER_MARKERS = ("server_error", "internal_error", "service_unavailable")
_INVALID_REQUEST_MARKERS = ("invalid_request", "bad_request")


class ModelRequestError(ModelError):
    """Backward-compatible model request error with structured information."""

    def __init__(
        self,
        message: str,
        *,
        kind: ModelErrorKind = "unknown",
        provider: str = "unknown",
        retryable: bool | None = None,
        status_code: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if retryable is None:
            retryable = kind in RETRYABLE_MODEL_ERROR_KINDS
        super().__init__(
            ModelErrorInfo(
                kind=kind,
                message=_safe_message(message),
                provider=provider,
                retryable=retryable,
                status_code=status_code,
                code=code,
                retry_after=retry_after,
                request_id=request_id,
                metadata=metadata or {},
            )
        )


async def iter_sse_data(
    response: httpx.Response,
    *,
    provider: str = "unknown",
) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON objects from a standard SSE response."""

    async for line in response.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ModelRequestError(
                f"Invalid SSE JSON: {payload[:200]}",
                kind="protocol",
                provider=provider,
                code="invalid_sse_json",
            ) from exc
        if not isinstance(value, dict):
            raise ModelRequestError(
                "SSE data must contain a JSON object",
                kind="protocol",
                provider=provider,
                code="invalid_sse_payload",
            )
        yield value


async def raise_for_model_status(
    response: httpx.Response,
    *,
    provider: str = "unknown",
) -> None:
    if not response.is_error:
        return
    raw = (await response.aread()).decode(errors="replace")
    payload = _parse_json_object(raw)
    message, code = _extract_error(payload, raw)
    kind = classify_model_error(
        status_code=response.status_code,
        code=code,
        message=message,
    )
    raise ModelRequestError(
        message,
        kind=kind,
        provider=provider,
        status_code=response.status_code,
        code=code,
        retry_after=parse_retry_after(response.headers.get("retry-after")),
        request_id=_request_id(response.headers),
    )


def model_error_from_payload(
    payload: Mapping[str, Any],
    *,
    provider: str,
    status_code: int | None = None,
    retry_after: float | None = None,
    request_id: str | None = None,
) -> ModelRequestError:
    message, code = _extract_error(dict(payload), "Model stream error")
    kind = classify_model_error(
        status_code=status_code,
        code=code,
        message=message,
    )
    return ModelRequestError(
        message,
        kind=kind,
        provider=provider,
        status_code=status_code,
        code=code,
        retry_after=retry_after,
        request_id=request_id,
    )


def normalize_model_exception(error: Exception, *, provider: str) -> ModelError:
    if isinstance(error, ModelError):
        return error
    if isinstance(error, httpx.TimeoutException):
        return ModelRequestError(
            f"Model request timed out: {error}",
            kind="timeout",
            provider=provider,
            code=type(error).__name__,
        )
    if isinstance(error, httpx.TransportError):
        return ModelRequestError(
            f"Model connection failed: {error}",
            kind="connection",
            provider=provider,
            code=type(error).__name__,
        )
    return ModelRequestError(
        f"Unexpected model error: {type(error).__name__}: {error}",
        kind="unknown",
        provider=provider,
        code=type(error).__name__,
    )


def classify_model_error(
    *,
    status_code: int | None,
    code: str | None,
    message: str,
) -> ModelErrorKind:
    text = f"{code or ''} {message}".lower()
    if any(marker in text for marker in _CONTEXT_MARKERS):
        return "context_overflow"
    if any(marker in text for marker in _QUOTA_MARKERS):
        return "quota_exhausted"
    if any(marker in text for marker in _OVERLOAD_MARKERS) or status_code == 529:
        return "overloaded"
    if status_code == 401 or any(marker in text for marker in _AUTHENTICATION_MARKERS):
        return "authentication"
    if status_code == 403 or any(marker in text for marker in _PERMISSION_MARKERS):
        return "permission"
    if status_code == 404 or any(marker in text for marker in _NOT_FOUND_MARKERS):
        return "not_found"
    if status_code in {408, 504} or any(marker in text for marker in _TIMEOUT_MARKERS):
        return "timeout"
    if status_code == 429 or any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    if (
        status_code is not None and 500 <= status_code <= 599
    ) or any(marker in text for marker in _SERVER_MARKERS):
        return "server"
    if status_code in {400, 405, 409, 413, 422} or any(
        marker in text for marker in _INVALID_REQUEST_MARKERS
    ):
        return "invalid_request"
    return "unknown"


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0.0, (parsed - reference).total_seconds())


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _extract_error(payload: dict[str, Any], fallback: str) -> tuple[str, str | None]:
    error: Any = payload.get("error", payload)
    if isinstance(error, dict):
        message = error.get("message") or payload.get("message") or fallback
        code = error.get("code") or error.get("type") or payload.get("code")
    else:
        message = error or payload.get("message") or fallback
        code = payload.get("code")
    return _safe_message(str(message)), str(code) if code is not None else None


def _request_id(headers: httpx.Headers) -> str | None:
    for name in (
        "request-id",
        "x-request-id",
        "anthropic-request-id",
        "openai-request-id",
    ):
        if value := headers.get(name):
            return value
    return None


def _safe_message(message: str) -> str:
    value = message.strip() or "Model request failed"
    return value[:_MESSAGE_LIMIT]


__all__ = [
    "ModelRequestError",
    "classify_model_error",
    "iter_sse_data",
    "model_error_from_payload",
    "normalize_model_exception",
    "parse_retry_after",
    "raise_for_model_status",
]
