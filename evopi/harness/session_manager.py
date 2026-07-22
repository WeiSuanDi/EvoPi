"""Lightweight in-memory session identity for the MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass(slots=True)
class SessionManager:
    session_id: str = field(default_factory=lambda: uuid4().hex)

    def reset(self) -> None:
        self.session_id = uuid4().hex


__all__ = ["SessionManager"]
