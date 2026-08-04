"""Validity proof for the interaction conformance kit (HIF-3).

The known-good reference adapter must pass every scenario, and every
deliberately broken mutant must fail exactly its intended scenario while
passing all the others.  This proves the kit's scenarios are sharp enough to
detect each defect in the acceptance matrix.
"""

from __future__ import annotations

import asyncio

import pytest

from .conformance import (
    CONFIRMATION_SCENARIOS,
    RPC_SCENARIOS,
    ConformanceFailure,
)
from .mutants import (
    CONFIRMATION_MUTANTS,
    RPC_MUTANTS,
    BlockedReaderRpcMutant,
    DuplicateAcceptConfirmationMutant,
    DuplicateDispatchRpcMutant,
    DuplicateReplayRpcMutant,
    ExceptionLeakRpcMutant,
    ExpiredResponseAcceptConfirmationMutant,
    PartialBatchConfirmationMutant,
    ReplayOrphanConfirmationMutant,
    SkippedEventRpcMutant,
    StaleCursorSilentSkipRpcMutant,
    TimeoutAsAbortConfirmationMutant,
)
from .reference import ReferenceConfirmationAdapter, ReferenceRpcAdapter


def _run(awaitable) -> None:
    asyncio.run(awaitable)


@pytest.mark.parametrize("scenario", list(CONFIRMATION_SCENARIOS))
def test_reference_passes_every_confirmation_scenario(scenario: str) -> None:
    _run(CONFIRMATION_SCENARIOS[scenario](ReferenceConfirmationAdapter()))


@pytest.mark.parametrize("scenario", list(RPC_SCENARIOS))
def test_reference_passes_every_rpc_scenario(scenario: str) -> None:
    _run(RPC_SCENARIOS[scenario](ReferenceRpcAdapter()))


@pytest.mark.parametrize("mutant", list(CONFIRMATION_MUTANTS))
def test_confirmation_mutant_fails_only_its_intended_scenario(mutant: str) -> None:
    factory, target = CONFIRMATION_MUTANTS[mutant]
    with pytest.raises(ConformanceFailure):
        _run(CONFIRMATION_SCENARIOS[target](factory()))
    for scenario, run in CONFIRMATION_SCENARIOS.items():
        if scenario == target:
            continue
        _run(run(factory()))


@pytest.mark.parametrize("mutant", list(RPC_MUTANTS))
def test_rpc_mutant_fails_only_its_intended_scenario(mutant: str) -> None:
    factory, target = RPC_MUTANTS[mutant]
    with pytest.raises(ConformanceFailure):
        _run(RPC_SCENARIOS[target](factory()))
    for scenario, run in RPC_SCENARIOS.items():
        if scenario == target:
            continue
        _run(run(factory()))


def test_acceptance_matrix_is_exactly_mapped() -> None:
    """The mutant-to-scenario mapping is exact, not merely inclusive."""
    expected_confirmation = {
        "replay-orphan": (ReplayOrphanConfirmationMutant, "orphan/no replay"),
        "partial-batch": (PartialBatchConfirmationMutant, "atomic batch"),
        "duplicate-accept": (DuplicateAcceptConfirmationMutant, "duplicate response"),
        "timeout-as-abort": (TimeoutAsAbortConfirmationMutant, "timeout/no execution"),
        "expired-accept": (ExpiredResponseAcceptConfirmationMutant, "stale/expired rejection"),
    }
    expected_rpc = {
        "skipped-event": (SkippedEventRpcMutant, "replay/live handoff"),
        "duplicate-replay": (DuplicateReplayRpcMutant, "event uniqueness"),
        "stale-cursor-silent-skip": (StaleCursorSilentSkipRpcMutant, "cursor expiration"),
        "blocked-reader": (BlockedReaderRpcMutant, "slow subscriber failure"),
        "duplicate-dispatch": (DuplicateDispatchRpcMutant, "duplicate request ID"),
        "exception-leak": (ExceptionLeakRpcMutant, "RPC redaction"),
    }
    assert CONFIRMATION_MUTANTS == expected_confirmation
    assert RPC_MUTANTS == expected_rpc
    scenarios = set(CONFIRMATION_SCENARIOS) | set(RPC_SCENARIOS)
    targets = {target for _, target in CONFIRMATION_MUTANTS.values()}
    targets |= {target for _, target in RPC_MUTANTS.values()}
    assert targets <= scenarios
    assert len(CONFIRMATION_SCENARIOS) == 8
    assert len(RPC_SCENARIOS) == 11
