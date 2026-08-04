"""Tests for the documented Integration entry point ``run_conformance`` (SFU-3).

Integration calls ``run_conformance(adapter_factory)`` with a factory that
builds a fresh production adapter and asserts every scenario reports ``"ok"``.
These tests prove the entry point itself: the reference factory is fully
green, and each mutant factory reports exactly its intended scenario as a
failure while every other scenario stays green.
"""

from __future__ import annotations

import pytest

from .conformance import INTERACTION_SCENARIOS, run_conformance
from .mutants import INTERACTION_MUTANTS
from .reference import ReferenceInteractionAdapter


def test_run_conformance_reference_is_fully_green() -> None:
    results = run_conformance(ReferenceInteractionAdapter)
    failures = {name: status for name, status in results.items() if status != "ok"}
    assert not failures, failures
    assert len(results) == len(INTERACTION_SCENARIOS)


@pytest.mark.parametrize("mutant", list(INTERACTION_MUTANTS))
def test_run_conformance_reports_exactly_the_mutant_target(mutant: str) -> None:
    factory, target = INTERACTION_MUTANTS[mutant]
    results = run_conformance(factory)
    assert results[target] != "ok"
    others = {name: status for name, status in results.items() if name != target}
    assert all(status == "ok" for status in others.values()), others
