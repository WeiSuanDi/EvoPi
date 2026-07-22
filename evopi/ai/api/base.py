"""Shared HTTP/SSE helpers for model adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class ModelRequestError(RuntimeError):
    pass


async def iter_sse_data(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
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
            raise ModelRequestError(f"Invalid SSE JSON: {payload[:200]}") from exc
        if isinstance(value, dict):
            yield value


async def raise_for_model_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = (await response.aread()).decode(errors="replace")[:1000]
        raise ModelRequestError(f"Model API returned HTTP {response.status_code}: {body}") from exc


__all__ = ["ModelRequestError", "iter_sse_data", "raise_for_model_status"]
