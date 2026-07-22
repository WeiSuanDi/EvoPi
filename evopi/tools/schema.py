"""Helpers for the JSON schemas exposed to language models."""

from __future__ import annotations

from typing import Any


def object_schema(
    properties: dict[str, dict[str, Any]],
    *,
    required: list[str] | None = None,
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required or []),
        "additionalProperties": additional_properties,
    }


__all__ = ["object_schema"]
