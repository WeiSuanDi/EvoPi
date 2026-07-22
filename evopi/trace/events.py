"""Serializable trace record."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True, kw_only=True)
class TraceRecord:
    type: str
    schema_version: int = 2
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


__all__ = ["TraceRecord"]
