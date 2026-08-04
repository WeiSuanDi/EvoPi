"""Fixture determinism and JSON-safety checks for the conformance kit (HIF-3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import pytest

from .conformance import (
    make_confirmation_request,
    make_response,
    to_json_safe,
)


def test_confirmation_fixtures_are_deterministic() -> None:
    first = make_confirmation_request(request_id="fixture-1", timeout_seconds=5.0)
    second = make_confirmation_request(request_id="fixture-1", timeout_seconds=5.0)
    assert first == second
    assert json.dumps(to_json_safe(first), sort_keys=True) == json.dumps(
        to_json_safe(second), sort_keys=True
    )


def test_confirmation_fixtures_are_synthetic_only() -> None:
    request = make_confirmation_request(request_id="fixture-2")
    assert request.arguments is not None
    assert request.arguments["command"] == "echo kit-fixture"
    assert request.arguments["secret"] == "kit-secret-value"
    assert request.arguments["path"] == "synthetic:/kit/path"
    assert request.policy_names == ("kit-test-policy",)
    assert request.run_id == "run-kit"
    assert request.session_id == "session-kit"
    response = make_response(request_id="fixture-2", decision="approve")
    assert response.request_id == "fixture-2"
    assert response.decision == "approve"


def test_to_json_safe_handles_kit_value_kinds() -> None:
    class Sample(Enum):
        ALPHA = "alpha"

    @dataclass(slots=True, frozen=True)
    class SampleData:
        name: str
        at: datetime

    fixed_ts = datetime(2026, 1, 1, tzinfo=UTC)
    converted = to_json_safe(SampleData(name="x", at=fixed_ts))
    assert converted == {"name": "x", "at": fixed_ts.isoformat()}
    assert to_json_safe(Path("a/b")) == str(Path("a/b"))
    assert to_json_safe(Sample.ALPHA) == "alpha"
    assert to_json_safe([1, {"x": None}]) == [1, {"x": None}]
    assert to_json_safe(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        to_json_safe(object())


def test_fixtures_never_depend_on_environment() -> None:
    # Same explicit arguments always produce identical content; nothing is
    # read from .env, real Trace, real Session, or user data.
    request = make_confirmation_request(request_id="fixture-3", timeout_seconds=30.0)
    expected = {
        "id": "fixture-3",
        "hook": "pre_tool_execution",
        "reason": "synthetic confirmation fixture",
        "risk_level": "high",
        "policy_names": ["kit-test-policy"],
        "arguments": {
            "command": "echo kit-fixture",
            "path": "synthetic:/kit/path",
            "secret": "kit-secret-value",
        },
        "metadata": {"fixture": "deterministic", "synthetic": True},
        "timeout_seconds": 30.0,
        "run_id": "run-kit",
        "session_id": "session-kit",
    }
    assert to_json_safe(request) == expected
