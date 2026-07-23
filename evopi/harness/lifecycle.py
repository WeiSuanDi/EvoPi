"""Small explicit lifecycle state machine."""

from __future__ import annotations

from evopi.core.run import AgentEndReason
from evopi.harness.runtime_state import LifecycleState, RuntimeState


class Lifecycle:
    def __init__(self) -> None:
        self.state = RuntimeState()

    def start(self) -> None:
        if self.state.status in {
            LifecycleState.RUNNING,
            LifecycleState.WAITING_FOR_CONFIRMATION,
            LifecycleState.ABORTING,
        }:
            raise RuntimeError("Harness is already running")
        self.state.status = LifecycleState.RUNNING
        self.state.end_reason = None
        self.state.last_error = None

    def complete(self, end_reason: AgentEndReason = "completed") -> None:
        self.state.status = LifecycleState.COMPLETED
        self.state.end_reason = end_reason

    def wait_for_confirmation(self) -> None:
        if self.state.status is not LifecycleState.RUNNING:
            raise RuntimeError("Harness must be running before it can wait for confirmation")
        self.state.status = LifecycleState.WAITING_FOR_CONFIRMATION

    def resume(self) -> None:
        if self.state.status is LifecycleState.ABORTING:
            return
        if self.state.status is not LifecycleState.WAITING_FOR_CONFIRMATION:
            raise RuntimeError("Harness is not waiting for confirmation")
        self.state.status = LifecycleState.RUNNING

    def request_abort(self) -> None:
        if self.state.status in {
            LifecycleState.RUNNING,
            LifecycleState.WAITING_FOR_CONFIRMATION,
        }:
            self.state.status = LifecycleState.ABORTING

    def abort(self, error: str | None = None) -> None:
        self.state.status = LifecycleState.ABORTED
        self.state.end_reason = "aborted"
        self.state.last_error = error

    def fail(
        self,
        exc: BaseException,
        end_reason: AgentEndReason = "error",
    ) -> None:
        self.state.status = LifecycleState.FAILED
        self.state.end_reason = end_reason
        self.state.last_error = f"{type(exc).__name__}: {exc}"

    def reset(self) -> None:
        self.state = RuntimeState()


__all__ = ["Lifecycle"]
