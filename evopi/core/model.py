"""Unified model contract consumed by EvoPi Core."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from evopi.core.context import AgentContext
from evopi.core.stream import ModelStreamEvent


@runtime_checkable
class Model(Protocol):
    @property
    def name(self) -> str: ...

    def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]: ...


__all__ = ["Model"]
