"""Small explicit lifecycle state machine."""

from __future__ import annotations

from evopi.harness.runtime_state import LifecycleState, RuntimeState


class Lifecycle:
    def __init__(self) -> None:
        self.state = RuntimeState()

    def start(self) -> None:
        if self.state.status is LifecycleState.RUNNING:
            raise RuntimeError("Harness is already running")
        self.state.status = LifecycleState.RUNNING
        self.state.last_error = None

    def complete(self) -> None:
        self.state.status = LifecycleState.COMPLETED

    def fail(self, exc: BaseException) -> None:
        self.state.status = LifecycleState.FAILED
        self.state.last_error = f"{type(exc).__name__}: {exc}"

    def reset(self) -> None:
        self.state = RuntimeState()


__all__ = ["Lifecycle"]
