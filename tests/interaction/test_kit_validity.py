"""Validity proof for the Confirmation conformance kit (HIF-3).

The known-good reference adapter must pass every scenario, and every
deliberately broken mutant must fail exactly its intended scenario while
passing all the others.  This proves the kit's scenarios are sharp enough to
detect each defect in the acceptance matrix.
"""

from __future__ import annotations

import asyncio

import pytest

from .conformance import CONFIRMATION_SCENARIOS, ConformanceFailure
from .mutants import CONFIRMATION_MUTANTS
from .reference import ReferenceConfirmationAdapter


def _run(awaitable) -> None:
    asyncio.run(awaitable)


@pytest.mark.parametrize("scenario", list(CONFIRMATION_SCENARIOS))
def test_reference_passes_every_confirmation_scenario(scenario: str) -> None:
    _run(CONFIRMATION_SCENARIOS[scenario](ReferenceConfirmationAdapter()))


@pytest.mark.parametrize("mutant", list(CONFIRMATION_MUTANTS))
def test_confirmation_mutant_fails_only_its_intended_scenario(mutant: str) -> None:
    factory, target = CONFIRMATION_MUTANTS[mutant]
    with pytest.raises(ConformanceFailure):
        _run(CONFIRMATION_SCENARIOS[target](factory()))
    for scenario, run in CONFIRMATION_SCENARIOS.items():
        if scenario == target:
            continue
        _run(run(factory()))


def test_acceptance_matrix_is_fully_covered() -> None:
    """Every mutant targets a registered scenario; the matrix has 5 confirmation mutants."""
    targets = {target for _, target in CONFIRMATION_MUTANTS.values()}
    assert targets <= set(CONFIRMATION_SCENARIOS)
    assert len(CONFIRMATION_MUTANTS) == 5
    assert len(CONFIRMATION_SCENARIOS) == 7
