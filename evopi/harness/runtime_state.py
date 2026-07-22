"""Public lifecycle snapshot for one Harness."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LifecycleState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    FAILED = "failed"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass(slots=True)
class RuntimeState:
    status: LifecycleState = LifecycleState.IDLE
    last_error: str | None = None


__all__ = ["LifecycleState", "RuntimeState"]
