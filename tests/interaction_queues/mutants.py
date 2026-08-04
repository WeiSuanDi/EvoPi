"""Deliberately broken mutant adapters for the conformance kit (SFU-3).

Each mutant breaks exactly one high-risk behavior of the reference adapter,
mirroring the acceptance matrix in the Task Packet one-to-one.  The validity
tests prove that (a) the known-good reference passes every scenario and (b)
every mutant fails only its intended scenario, so the kit's scenarios are
sharp enough to detect the defect they claim to detect.

Mutant contracts (acceptance matrix):

- ``SkipSiblingToolsMutant`` drains pending steering as soon as the first
  sibling Tool of the batch finishes, instead of after the complete batch and
  ``turn_end``.
- ``FollowUpEveryTurnMutant`` drains follow-up at every ``turn_end`` safe
  point instead of only at terminal candidates.
- ``AllSnapshotRescanMutant`` re-scans the live follow-up queue while draining
  instead of delivering one atomic ``all`` snapshot.
- ``AcknowledgeAfterSealMutant`` returns a receipt even after the terminal
  seal won the admission race.
- ``PersistQueuedContentMutant`` writes queued-but-undelivered content into
  the Session projection.
- ``ContentInEventsMutant`` duplicates interaction content into queue events
  and therefore into Trace.
- ``ContinueAfterTerminalMutant`` delivers pending input after Abort, deadline,
  error, or Turn exhaustion instead of clearing it fail closed.
- ``RetryAsTurnMutant`` treats a provider retry as another Turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeAlias

from .conformance import (
    AdmissionResult,
    CommittedMessage,
    EventType,
    InteractionKind,
    InteractionLimits,
    InteractionQueueMode,
    TerminalReason,
)
from .reference import ReferenceInteractionAdapter

MutantFactory: TypeAlias = Callable[[], ReferenceInteractionAdapter]


class SkipSiblingToolsMutant(ReferenceInteractionAdapter):
    """MUTANT: delivers steering after the first sibling Tool result."""

    async def tool_step(self, index: int, *, confirmation: bool = False) -> None:
        if index == 0:
            await super().tool_step(index, confirmation=confirmation)
            if self._steering_queue:
                # broken: safe point before the remaining siblings execute
                await self._drain("steer")
            return
        await super().tool_step(index, confirmation=confirmation)


class FollowUpEveryTurnMutant(ReferenceInteractionAdapter):
    """MUTANT: drains follow-up at every turn safe point."""

    async def turn_end(self, *, terminate: bool = False) -> str:
        outcome = await super().turn_end(terminate=terminate)
        if self._follow_up_queue:
            # broken: follow-up must only drain at a terminal candidate
            await self._drain("follow_up")
            return "continued"
        return outcome


class AllSnapshotRescanMutant(ReferenceInteractionAdapter):
    """MUTANT: the follow-up drain re-scans the live queue until empty."""

    async def _drain_follow_up(self) -> int:
        # broken: drains whatever is in the queue at each step, so arrivals
        # during the drain join the current batch instead of the next one
        count = 0
        while self._follow_up_queue:
            item = self._follow_up_queue.pop(0)
            await asyncio.sleep(0)
            self._deliver(item)
            count += 1
        if count:
            self._emit("turn_start", {"turn_number": self._turns + 1})
            self._turns += 1
        return count


def make_all_snapshot_mutant() -> AllSnapshotRescanMutant:
    """Factory for the atomic-snapshot mutant with ``all`` follow-up mode."""
    return AllSnapshotRescanMutant(follow_up_mode="all")


class AcknowledgeAfterSealMutant(ReferenceInteractionAdapter):
    """MUTANT: admission is acknowledged even after the terminal seal."""

    def _admit(self, kind: InteractionKind, content: Any, origin: Any) -> AdmissionResult:
        if self._run_id is None:
            return super()._admit(kind, content, origin)
        # broken: the seal check is skipped, so a sealed Run still returns a
        # receipt for an item that can never be delivered or cleared
        return self._admit_open(kind, content, origin)


class PersistQueuedContentMutant(ReferenceInteractionAdapter):
    """MUTANT: writes queued-but-undelivered content into the Session."""

    async def _drain(self, kind: InteractionKind) -> int:
        drained = await super()._drain(kind)
        self._persist_pending()
        return drained

    async def terminate(self, reason: TerminalReason) -> None:
        self._persist_pending()
        await super().terminate(reason)

    def _persist_pending(self) -> None:
        # broken: everything still queued is persisted as a plain UserMessage
        for item in list(self._steering_queue) + list(self._follow_up_queue):
            self._message_counter += 1
            self._committed.append(
                CommittedMessage(
                    message_id=f"msg-{self._message_counter}",
                    role="user",
                    content=item.content,
                    interaction=None,
                )
            )


class ContentInEventsMutant(ReferenceInteractionAdapter):
    """MUTANT: queue events duplicate the interaction content."""

    def __init__(
        self,
        *,
        steering_mode: InteractionQueueMode = "one-at-a-time",
        follow_up_mode: InteractionQueueMode = "one-at-a-time",
        limits: InteractionLimits | None = None,
    ) -> None:
        super().__init__(
            steering_mode=steering_mode,
            follow_up_mode=follow_up_mode,
            limits=limits,
        )
        self._leaked_content: str = ""

    def _admit_open(self, kind: InteractionKind, content: Any, origin: Any) -> AdmissionResult:
        result = super()._admit_open(kind, content, origin)
        if result.receipt is not None:
            self._leaked_content = str(content)
        return result

    def _emit(self, type_: EventType, data: dict[str, Any]) -> None:
        if type_ in ("interaction_queued", "interaction_delivered", "interaction_cleared"):
            # broken: event data must never carry interaction content
            data = dict(data)
            data["content"] = self._leaked_content
        super()._emit(type_, data)


class ContinueAfterTerminalMutant(ReferenceInteractionAdapter):
    """MUTANT: delivers pending input after a terminal reason."""

    async def terminate(self, reason: TerminalReason) -> None:
        # broken: Abort/deadline/error/turn-limit must clear, not deliver
        if self._steering_queue:
            await self._drain("steer")
        if self._follow_up_queue:
            await self._drain("follow_up")
        await super().terminate(reason)


class RetryAsTurnMutant(ReferenceInteractionAdapter):
    """MUTANT: a provider retry consumes another Turn."""

    async def model_stream(self, *, tool_calls: int = 0, retry: bool = False) -> str:
        if retry:
            # broken: a retry is a Model Attempt, not another Turn
            self._turns += 1
        return await super().model_stream(tool_calls=tool_calls, retry=retry)


INTERACTION_MUTANTS: dict[str, tuple[MutantFactory, str]] = {
    "skip-sibling-tools": (SkipSiblingToolsMutant, "steering during first sibling tool"),
    "follow-up-every-turn": (FollowUpEveryTurnMutant, "follow-up during tool continuation"),
    "all-snapshot-rescan": (make_all_snapshot_mutant, "atomic drain snapshot"),
    "acknowledge-after-seal": (AcknowledgeAfterSealMutant, "enqueue vs terminal seal"),
    "persist-queued-content": (PersistQueuedContentMutant, "session projection"),
    "content-in-events": (ContentInEventsMutant, "trace redaction"),
    "continue-after-terminal": (ContinueAfterTerminalMutant, "terminal priority"),
    "retry-as-turn": (RetryAsTurnMutant, "retry is not a turn"),
}
