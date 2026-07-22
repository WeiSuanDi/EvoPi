"""Small dependency-free types shared across EvoPi Core."""

from __future__ import annotations

from typing import Any, TypeAlias

JsonObject: TypeAlias = dict[str, Any]
Metadata: TypeAlias = dict[str, Any]

__all__ = ["JsonObject", "Metadata"]
