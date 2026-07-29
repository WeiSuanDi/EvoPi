"""Harness-owned ordered routing around the provider-neutral Core executor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from evopi.ai.routing import (
    CircuitStateSnapshot,
    ModelCandidate,
    ModelRoute,
    ModelRouteUnavailableError,
)
from evopi.core.cancellation import AbortSignal
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.model_attempts import (
    ModelAttemptInfo,
    ModelAttemptSelection,
)
from evopi.core.model_errors import ModelErrorInfo, ModelRetryConfig
from evopi.session.compact import estimate_context_tokens

FailoverAuthorizer = Callable[
    [
        ModelAttemptInfo | None,
        ModelAttemptInfo,
        ModelErrorInfo | None,
        int,
        tuple[CircuitStateSnapshot, ...],
        str,
        AgentContext,
        AbortSignal | None,
    ],
    Awaitable[None],
]
EventEmitter = Callable[[CoreEvent], Awaitable[None]]


class HarnessModelAttemptRouter:
    """One Run-scoped route view backed by a shared ModelRoute circuit."""

    def __init__(
        self,
        *,
        route: ModelRoute,
        retry_config: ModelRetryConfig,
        run_id: str,
        system_prompt: str,
        authorize_failover: FailoverAuthorizer,
        emit: EventEmitter,
    ) -> None:
        self.route = route
        self.retry_config = retry_config
        self.run_id = run_id
        self.system_prompt = system_prompt
        self._authorize_failover = authorize_failover
        self._emit = emit
        self._attempted: set[str] = set()
        self._acquired: set[str] = set()
        self._affinity_info: ModelAttemptInfo | None = None
        self._pending_authorizations: dict[
            ModelAttemptInfo,
            tuple[ModelAttemptInfo | None, ModelErrorInfo | None, int, str],
        ] = {}
        self._route_round = 1
        self._order: tuple[ModelCandidate, ...] = route.candidates

    async def select_initial(
        self,
        *,
        context: AgentContext,
        attempt: int,
        run_id: str | None,
        turn: int,
        signal: AbortSignal | None,
    ) -> ModelAttemptSelection:
        self._attempted.clear()
        self._route_round = 1
        affinity = self.route.affinity_for(self.run_id)
        self._order = self._ordered_candidates()
        selection, selection_reason = await self._select(
            context=context,
            attempt=attempt,
            turn=turn,
        )
        if selection is None:
            raise self._unavailable_error("No healthy compatible model candidate")
        expected_candidate_id = affinity or self.route.primary.candidate_id
        if selection.info.candidate_id != expected_candidate_id:
            self._pending_authorizations[selection.info] = (
                self._affinity_info if affinity is not None else None,
                None,
                (
                    self.retry_config.max_retries
                    if self.retry_config.enabled
                    else 0
                ),
                selection_reason or "primary_unavailable",
            )
        return selection

    async def select_after_failure(
        self,
        *,
        context: AgentContext,
        previous: ModelAttemptSelection,
        error: Exception,
        error_info: ModelErrorInfo | None,
        next_attempt: int,
        max_attempts: int,
        run_id: str | None,
        turn: int,
        signal: AbortSignal | None,
    ) -> ModelAttemptSelection | None:
        del error, run_id
        if error_info is None:
            return None
        if not self.route.failover_config.can_failover(error_info):
            if not error_info.retryable:
                return None
            delay = self._retry_delay(error_info, next_attempt - 1)
            if delay is None:
                return None
            candidate_id = previous.info.candidate_id
            acquired = self.route.acquire(candidate_id)
            next_info = replace(previous.info, attempt=next_attempt)
            if acquired.before != acquired.after:
                await self._emit_circuit_change(
                    acquired.before,
                    acquired.after,
                    next_info,
                )
            if not acquired.acquired:
                raise self._unavailable_error(
                    "The selected model candidate circuit is open"
                )
            self._acquired.add(candidate_id)
            return ModelAttemptSelection(
                model=previous.model,
                info=next_info,
                delay=delay,
            )

        self._attempted.add(previous.info.candidate_id)
        selection, _ = await self._select(
            context=context,
            attempt=next_attempt,
            turn=turn,
        )
        if selection is None:
            self._route_round += 1
            self._attempted.clear()
            selection, _ = await self._select(
                context=context,
                attempt=next_attempt,
                turn=turn,
            )
            if selection is None:
                raise self._unavailable_error(
                    "All model candidates are unavailable or context-incompatible"
                )
            delay = self._retry_delay(error_info, next_attempt - 1)
            if delay is None:
                await self.record_abandoned(selection)
                return None
            selection = ModelAttemptSelection(
                model=selection.model,
                info=selection.info,
                delay=delay,
            )

        if selection.info.candidate_id != previous.info.candidate_id:
            self._pending_authorizations[selection.info] = (
                previous.info,
                error_info,
                max(0, max_attempts - next_attempt),
                "attempt_failed",
            )
        return selection

    async def authorize_attempt(
        self,
        selection: ModelAttemptSelection,
        context: AgentContext,
        signal: AbortSignal | None,
    ) -> None:
        pending = self._pending_authorizations.pop(selection.info, None)
        if pending is None:
            return
        source, error_info, remaining_attempts, selection_reason = pending
        await self._authorize_transition(
            source=source,
            target=selection,
            error_info=error_info,
            remaining_attempts=remaining_attempts,
            selection_reason=selection_reason,
            context=context,
            signal=signal,
        )

    async def record_failure(
        self,
        selection: ModelAttemptSelection,
        error: Exception,
        error_info: ModelErrorInfo | None,
    ) -> None:
        del error
        candidate_id = selection.info.candidate_id
        before = self.route.circuit_snapshot(candidate_id)
        if error_info is not None:
            self.route.record_failure(candidate_id, error_info)
        else:
            self.route.release(candidate_id)
        self._acquired.discard(candidate_id)
        after = self.route.circuit_snapshot(candidate_id)
        if before != after:
            await self._emit_circuit_change(before, after, selection.info)

    async def record_success(self, selection: ModelAttemptSelection) -> None:
        before = self.route.circuit_snapshot(selection.info.candidate_id)
        self.route.record_success(selection.info.candidate_id)
        self.route.set_affinity(self.run_id, selection.info.candidate_id)
        self._affinity_info = selection.info
        self._acquired.discard(selection.info.candidate_id)
        after = self.route.circuit_snapshot(selection.info.candidate_id)
        if before != after:
            await self._emit_circuit_change(before, after, selection.info)

    async def record_abandoned(self, selection: ModelAttemptSelection) -> None:
        self.route.release(selection.info.candidate_id)
        self._acquired.discard(selection.info.candidate_id)

    async def close(self) -> None:
        for candidate_id in tuple(self._acquired):
            self.route.release(candidate_id)
        self._acquired.clear()
        self._pending_authorizations.clear()
        self.route.clear_affinity(self.run_id)
        self._affinity_info = None

    def _ordered_candidates(self) -> tuple[ModelCandidate, ...]:
        affinity = self.route.affinity_for(self.run_id)
        if not self.route.failover_config.enabled:
            selected_id = affinity or self.route.primary.candidate_id
            return tuple(
                candidate
                for candidate in self.route.candidates
                if candidate.candidate_id == selected_id
            )
        if affinity is None:
            return self.route.candidates
        index = next(
            (
                index
                for index, candidate in enumerate(self.route.candidates)
                if candidate.candidate_id == affinity
            ),
            0,
        )
        return self.route.candidates[index:] + self.route.candidates[:index]

    async def _select(
        self,
        *,
        context: AgentContext,
        attempt: int,
        turn: int,
    ) -> tuple[ModelAttemptSelection | None, str | None]:
        context_tokens = estimate_context_tokens(
            context.messages,
            system_prompt=self.system_prompt,
            tools=[tool.definition() for tool in context.tools],
        )
        first_skip_reason: str | None = None
        for candidate in self._order:
            if candidate.candidate_id in self._attempted:
                continue
            context_window = candidate.context_window or 0
            if (
                context_window > 0
                and context_tokens + candidate.output_reserve > context_window
            ):
                self._attempted.add(candidate.candidate_id)
                if first_skip_reason is None:
                    first_skip_reason = "context_incompatible"
                await self._emit(
                    CoreEvent(
                        type="model_candidate_skipped",
                        run_id=self.run_id,
                        data={
                            "candidate_id": candidate.candidate_id,
                            "provider": candidate.provider,
                            "model": candidate.model.name,
                            "failure_domain_id": candidate.failure_domain_id,
                            "reason": "context_incompatible",
                            "context_tokens": context_tokens,
                            "context_window": context_window,
                            "output_reserve": candidate.output_reserve,
                        },
                    )
                )
                continue
            acquired = self.route.acquire(candidate.candidate_id)
            if not acquired.acquired:
                self._attempted.add(candidate.candidate_id)
                if first_skip_reason is None:
                    first_skip_reason = "circuit_open"
                await self._emit(
                    CoreEvent(
                        type="model_candidate_skipped",
                        run_id=self.run_id,
                        data={
                            "candidate_id": candidate.candidate_id,
                            "provider": candidate.provider,
                            "model": candidate.model.name,
                            "failure_domain_id": candidate.failure_domain_id,
                            "reason": "circuit_open",
                            "circuit": self.route.circuit_snapshot(
                                candidate.candidate_id
                            ),
                        },
                    )
                )
                continue
            self._acquired.add(candidate.candidate_id)
            info = ModelAttemptInfo(
                route_id=self.route.route_id,
                candidate_id=candidate.candidate_id,
                provider=candidate.provider,
                model=candidate.model.name,
                failure_domain_id=candidate.failure_domain_id,
                attempt=attempt,
                route_round=self._route_round,
            )
            if acquired.before != acquired.after:
                await self._emit_circuit_change(
                    acquired.before,
                    acquired.after,
                    info,
                )
            return ModelAttemptSelection(model=candidate.model, info=info), first_skip_reason
        return None, first_skip_reason

    async def _authorize_transition(
        self,
        *,
        source: ModelAttemptInfo | None,
        target: ModelAttemptSelection,
        error_info: ModelErrorInfo | None,
        remaining_attempts: int,
        selection_reason: str,
        context: AgentContext,
        signal: AbortSignal | None,
    ) -> None:
        snapshots = tuple(
            self.route.circuit_snapshot(candidate.candidate_id)
            for candidate in self.route.candidates
        )
        event_data = {
            "source": source,
            "target": target.info,
            "error_info": error_info,
            "remaining_attempts": remaining_attempts,
            "selection_reason": selection_reason,
            "circuit_snapshots": snapshots,
        }
        await self._emit(
            CoreEvent(
                type="model_failover_start",
                run_id=self.run_id,
                data=event_data,
            )
        )
        try:
            await self._authorize_failover(
                source,
                target.info,
                error_info,
                remaining_attempts,
                snapshots,
                selection_reason,
                context,
                signal,
            )
        except BaseException as exc:
            await self.record_abandoned(target)
            await self._emit(
                CoreEvent(
                    type="model_failover_end",
                    run_id=self.run_id,
                    data={
                        **event_data,
                        "approved": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            )
            raise
        await self._emit(
            CoreEvent(
                type="model_failover_end",
                run_id=self.run_id,
                data={**event_data, "approved": True},
            )
        )

    async def _emit_circuit_change(
        self,
        before: CircuitStateSnapshot,
        after: CircuitStateSnapshot,
        attempt: ModelAttemptInfo,
    ) -> None:
        await self._emit(
            CoreEvent(
                type="model_circuit_state_changed",
                run_id=self.run_id,
                data={
                    "attempt_info": attempt,
                    "before": before,
                    "after": after,
                },
            )
        )

    def _unavailable_error(self, reason: str) -> ModelRouteUnavailableError:
        cooldowns = [
            snapshot.remaining_cooldown
            for snapshot in (
                self.route.circuit_snapshot(candidate.candidate_id)
                for candidate in self.route.candidates
            )
            if snapshot.state == "open" and snapshot.remaining_cooldown > 0
        ]
        return ModelRouteUnavailableError(
            route_id=self.route.route_id,
            earliest_retry_after=min(cooldowns) if cooldowns else None,
            reason=reason,
        )

    def _retry_delay(
        self,
        error_info: ModelErrorInfo,
        retry_number: int,
    ) -> float | None:
        if (
            error_info.retry_after is not None
            and error_info.retry_after > self.retry_config.max_delay
        ):
            return None
        local_delay = min(
            self.retry_config.base_delay * (2 ** (retry_number - 1)),
            self.retry_config.max_delay,
        )
        return max(local_delay, error_info.retry_after or 0.0)


__all__ = ["HarnessModelAttemptRouter"]
