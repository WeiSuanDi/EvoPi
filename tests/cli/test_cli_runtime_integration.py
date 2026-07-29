from __future__ import annotations

import argparse

import pytest

from evopi.ai import ModelRoute
from evopi.cli import main as exported_main
from evopi.cli.main import build_parser
from evopi.cli.runtime import build_model_runtime, parse_tool_selection


def _args(**overrides):
    values = {
        "provider": "anthropic",
        "model": "primary-model",
        "fallback": None,
        "no_failover": False,
        "circuit_failure_threshold": 2,
        "circuit_recovery_timeout": 30.0,
        "model_timeout": 10.0,
        "context_window": 1000,
        "max_output_tokens": 200,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_runtime_parser_exposes_failover_and_tool_ceiling() -> None:
    args = build_parser().parse_args(
        [
            "--fallback",
            "openai-responses:gpt-a",
            "--fallback",
            "anthropic:claude-b",
            "--circuit-failure-threshold",
            "4",
            "--circuit-recovery-timeout",
            "12",
            "--tools",
            "read_file,list_dir",
        ]
    )

    assert args.fallback == ["openai-responses:gpt-a", "anthropic:claude-b"]
    assert args.circuit_failure_threshold == 4
    assert args.circuit_recovery_timeout == 12
    assert parse_tool_selection(args) == ({"read_file", "list_dir"}, None)

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--tools", "read_file", "--exclude-tools", "write_file"]
        )


def test_build_model_runtime_creates_stable_ordered_route(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "configured")
    monkeypatch.setenv("OPENAI_API_KEY", "configured")

    primary, route, configs = build_model_runtime(
        _args(
            fallback=[
                "openai-responses:gpt-a",
                "openai-compatible:gpt-b",
            ]
        )
    )

    assert isinstance(route, ModelRoute)
    assert route.primary.model is primary
    assert [item.candidate_id for item in route.candidates] == [
        "primary",
        "fallback-1",
        "fallback-2",
    ]
    assert [item.provider for item in route.candidates] == [
        "anthropic",
        "openai-responses",
        "openai-compatible",
    ]
    assert tuple(config.model for config in configs) == (
        "primary-model",
        "gpt-a",
        "gpt-b",
    )


def test_fallback_validation_happens_before_models_are_constructed(
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "evopi.cli.runtime.model_from_config",
        lambda config, **kwargs: calls.append(config.model),
    )

    with pytest.raises(ValueError, match="provider:model"):
        build_model_runtime(_args(fallback=["broken"]))
    with pytest.raises(ValueError, match="cannot be combined"):
        build_model_runtime(
            _args(
                fallback=["openai-compatible:gpt-a"],
                no_failover=True,
            )
        )
    with pytest.raises(ValueError, match="Duplicate model candidate"):
        build_model_runtime(_args(fallback=["anthropic:primary-model"]))
    assert calls == []


def test_environment_fallbacks_are_used_only_when_cli_is_absent(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "configured")
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.setenv(
        "EVOPI_FALLBACKS",
        "openai-compatible:env-model",
    )

    _, env_route, _ = build_model_runtime(_args(fallback=None))
    _, cli_route, _ = build_model_runtime(
        _args(fallback=["openai-responses:cli-model"])
    )

    assert env_route is not None
    assert env_route.candidates[1].model.name == "env-model"
    assert cli_route is not None
    assert cli_route.candidates[1].model.name == "cli-model"
    assert exported_main is not None


def test_no_fallback_keeps_single_model_runtime(monkeypatch) -> None:
    monkeypatch.delenv("EVOPI_FALLBACKS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "configured")

    primary, route, configs = build_model_runtime(_args(fallback=None))

    assert route is None
    assert primary.name == "primary-model"
    assert len(configs) == 1
