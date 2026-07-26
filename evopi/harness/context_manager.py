"""Composable providers that prepare a model-call context snapshot."""

from __future__ import annotations

import inspect
from typing import Awaitable, Callable, TypeAlias

from evopi.core.cancellation import AbortSignal, call_with_optional_signal
from evopi.core.context import AgentContext

ContextProvider: TypeAlias = Callable[
    [AgentContext], Awaitable[AgentContext | None] | AgentContext | None
]


class ContextManager:
    def __init__(self) -> None:
        self._providers: list[ContextProvider] = []

    def add(self, provider: ContextProvider) -> None:
        self._providers.append(provider)

    def remove(self, provider: ContextProvider) -> None:
        try:
            self._providers.remove(provider)
        except ValueError:
            return

    async def prepare(
        self,
        context: AgentContext,
        *,
        signal: AbortSignal | None = None,
    ) -> AgentContext:
        current = context
        for provider in tuple(self._providers):
            replacement = call_with_optional_signal(provider, current, signal=signal)
            if inspect.isawaitable(replacement):
                replacement = await replacement
            if replacement is not None:
                current = replacement
        return current


__all__ = ["ContextManager", "ContextProvider"]
