"""Unified model contract consumed by EvoPi Core."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from evopi.core.cancellation import AbortSignal
from evopi.core.context import AgentContext
from evopi.core.stream import ModelStreamEvent


@runtime_checkable
class Model(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def context_window(self) -> int:
        """Maximum context tokens the model accepts.  0 = unknown."""
        return 0

    def stream(
        self,
        context: AgentContext,
        *,
        signal: AbortSignal | None = None,
    ) -> AsyncIterator[ModelStreamEvent]: ...


__all__ = ["Model"]
