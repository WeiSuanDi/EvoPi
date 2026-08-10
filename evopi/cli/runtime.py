"""Safe CLI-to-Harness runtime configuration."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from dotenv import load_dotenv

from evopi.ai import (
    CircuitBreakerConfig,
    ModelCandidate,
    ModelEnvironmentConfig,
    ModelFailoverConfig,
    ModelRoute,
    model_from_config,
)
from evopi.cli.model_configuration import resolve_cli_model_configuration
from evopi.core.model import Model


def fallback_values_from_args(args: argparse.Namespace) -> tuple[str, ...]:
    """Resolve CLI fallbacks, or the dotenv-backed environment default."""

    explicit = getattr(args, "fallback", None)
    if explicit is not None:
        return tuple(explicit)
    load_dotenv()
    raw = os.getenv("EVOPI_FALLBACKS", "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse_fallback(value: str) -> tuple[str, str]:
    provider, separator, model = value.partition(":")
    provider = provider.strip()
    model = model.strip()
    if not separator or not provider or not model:
        raise ValueError(
            f"Fallback '{value}' must use the provider:model format"
        )
    return provider, model


def build_model_runtime(
    args: argparse.Namespace,
) -> tuple[Model, ModelRoute | None, tuple[ModelEnvironmentConfig, ...]]:
    """Build the primary model and optional explicit failover route."""

    fallback_values = fallback_values_from_args(args)
    if fallback_values and getattr(args, "no_failover", False):
        raise ValueError("--fallback cannot be combined with --no-failover")

    primary_resolved = resolve_cli_model_configuration(
        getattr(args, "provider", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        require_complete=True,
    )
    fallback_resolved = tuple(
        resolve_cli_model_configuration(provider, model=model, require_complete=True)
        for provider, model in map(_parse_fallback, fallback_values)
    )
    resolved_configs = (primary_resolved, *fallback_resolved)
    primary_config = primary_resolved.safe
    fallback_configs = tuple(item.safe for item in fallback_resolved)
    configs = (primary_config, *fallback_configs)
    identities = [
        (config.provider, config.model, config.base_url)
        for config in configs
    ]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate model candidate in the configured route")

    timeout = getattr(args, "model_timeout", 120.0)
    context_window = getattr(args, "context_window", 0) or 0
    max_tokens = getattr(args, "max_output_tokens", 4096)
    models = tuple(
        model_from_config(
            resolved.safe,
            api_key=resolved.api_key,
            timeout=timeout,
            context_window=context_window,
            max_tokens=max_tokens,
        )
        for resolved in resolved_configs
    )
    primary = models[0]
    if len(models) == 1:
        return primary, None, configs

    candidates = tuple(
        ModelCandidate(
            candidate_id=(
                "primary" if index == 0 else f"fallback-{index}"
            ),
            provider=config.provider,
            model=model,
            failure_domain=f"{config.provider}|{config.base_url}",
            context_window=context_window,
            output_reserve=max_tokens,
        )
        for index, (config, model) in enumerate(zip(configs, models))
    )
    route = ModelRoute(
        candidates=candidates,
        failover_config=ModelFailoverConfig(enabled=True),
        circuit_config=CircuitBreakerConfig(
            failure_threshold=getattr(
                args,
                "circuit_failure_threshold",
                2,
            ),
            recovery_timeout=getattr(
                args,
                "circuit_recovery_timeout",
                30.0,
            ),
        ),
    )
    return primary, route, configs


def _parse_tool_names(raw: str) -> set[str]:
    names = [name.strip() for name in raw.split(",")]
    if not names or any(not name for name in names):
        raise ValueError("Tool selection must be a comma-separated list of names")
    if len(set(names)) != len(names):
        raise ValueError("Tool selection contains duplicate names")
    return set(names)


def parse_tool_selection(
    args: argparse.Namespace,
) -> tuple[set[str] | None, set[str] | None]:
    """Return the user-owned include/exclude ceiling selection."""

    included = getattr(args, "tools", None)
    excluded = getattr(args, "exclude_tools", None)
    if included is not None and excluded is not None:
        raise ValueError("--tools and --exclude-tools are mutually exclusive")
    return (
        _parse_tool_names(included) if included is not None else None,
        _parse_tool_names(excluded) if excluded is not None else None,
    )


def parse_fallback_specs(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """Public deterministic parser used by diagnostics and tests."""

    return tuple(_parse_fallback(value) for value in values)


__all__ = [
    "build_model_runtime",
    "fallback_values_from_args",
    "parse_fallback_specs",
    "parse_tool_selection",
]
