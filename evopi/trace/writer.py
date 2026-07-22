"""Append-only JSONL trace writer."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, cast

from evopi.core.events import CoreEvent
from evopi.trace.events import TraceRecord


class JsonlTraceWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def __call__(self, event: CoreEvent) -> None:
        self.write(
            TraceRecord(
                type=event.type,
                data=event.data,
                run_id=event.run_id,
                created_at=event.created_at,
            )
        )

    def write(self, record: TraceRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "type": record.type,
            "run_id": record.run_id,
            "created_at": record.created_at.isoformat(),
            "data": to_jsonable(record.data),
        }
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            key: to_jsonable(item)
            for key, item in asdict(cast(Any, value)).items()
        }
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return repr(value)


__all__ = ["JsonlTraceWriter", "to_jsonable"]
