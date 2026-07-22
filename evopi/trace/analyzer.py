"""Small trace summaries used by tests and future evolution tooling."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any


def count_event_types(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(record.get("type", "unknown")) for record in records))


__all__ = ["count_event_types"]
