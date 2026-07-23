"""Structured failures raised by the Session persistence layer."""

from __future__ import annotations


class SessionError(RuntimeError):
    """Base class for Session lifecycle and persistence failures."""


class SessionFormatError(SessionError):
    def __init__(self, reason: str, *, line_number: int | None = None) -> None:
        self.reason = reason
        self.line_number = line_number
        location = f" at line {line_number}" if line_number is not None else ""
        super().__init__(f"Invalid Session data{location}: {reason}")


class SessionLockError(SessionError):
    """Raised when another process already owns the Session."""


class SessionPersistenceError(SessionError):
    """Raised when an authoritative Session Log write fails."""


class SessionSerializationError(SessionError):
    """Raised when runtime state cannot be represented losslessly as JSON."""


__all__ = [
    "SessionError",
    "SessionFormatError",
    "SessionLockError",
    "SessionPersistenceError",
    "SessionSerializationError",
]
