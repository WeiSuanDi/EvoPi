"""Validity proof for the interaction conformance kit (SFU-3).

The known-good reference adapter must pass every scenario, and every
deliberately broken mutant must fail exactly its intended scenario while
passing all the others.  This proves the kit's scenarios are sharp enough to
detect each defect in the Task acceptance matrix and no other.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from .conformance import INTERACTION_SCENARIOS, ConformanceFailure
from .mutants import (
    AcknowledgeAfterSealMutant,
    AllSnapshotRescanMutant,
    ContentInEventsMutant,
    ContinueAfterTerminalMutant,
    FollowUpEveryTurnMutant,
    INTERACTION_MUTANTS,
    PersistQueuedContentMutant,
    RetryAsTurnMutant,
    SkipSiblingToolsMutant,
    make_all_snapshot_mutant,
)
from .reference import ReferenceInteractionAdapter


def _run(awaitable: Coroutine[Any, Any, None]) -> None:
    asyncio.run(awaitable)


@pytest.mark.parametrize("scenario", list(INTERACTION_SCENARIOS))
def test_reference_passes_every_scenario(scenario: str) -> None:
    _run(INTERACTION_SCENARIOS[scenario](ReferenceInteractionAdapter()))


@pytest.mark.parametrize("mutant", list(INTERACTION_MUTANTS))
def test_mutant_fails_only_its_intended_scenario(mutant: str) -> None:
    factory, target = INTERACTION_MUTANTS[mutant]
    with pytest.raises(ConformanceFailure):
        _run(INTERACTION_SCENARIOS[target](factory()))
    for scenario, run in INTERACTION_SCENARIOS.items():
        if scenario == target:
            continue
        _run(run(factory()))


def test_acceptance_matrix_is_exactly_mapped() -> None:
    """The mutant-to-scenario mapping mirrors the Task acceptance matrix
    exactly, not merely inclusively."""
    expected = {
        "skip-sibling-tools": (SkipSiblingToolsMutant, "steering during first sibling tool"),
        "follow-up-every-turn": (FollowUpEveryTurnMutant, "follow-up during tool continuation"),
        "all-snapshot-rescan": (make_all_snapshot_mutant, "atomic drain snapshot"),
        "acknowledge-after-seal": (AcknowledgeAfterSealMutant, "enqueue vs terminal seal"),
        "persist-queued-content": (PersistQueuedContentMutant, "session projection"),
        "content-in-events": (ContentInEventsMutant, "trace redaction"),
        "continue-after-terminal": (ContinueAfterTerminalMutant, "terminal priority"),
        "retry-as-turn": (RetryAsTurnMutant, "retry is not a turn"),
    }
    assert INTERACTION_MUTANTS == expected
    targets = {target for _, target in INTERACTION_MUTANTS.values()}
    assert targets <= set(INTERACTION_SCENARIOS)
    assert len(INTERACTION_SCENARIOS) == 27
    assert len(INTERACTION_MUTANTS) == 8
    # the mode-specific mutant factory really constructs the all-mode adapter
    adapter = make_all_snapshot_mutant()
    assert isinstance(adapter, AllSnapshotRescanMutant)
    assert adapter.snapshot().follow_up_mode == "all"
