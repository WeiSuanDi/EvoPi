"""Credential resolution without persistence or logging."""

from __future__ import annotations

import os


def resolve_api_key(explicit: str | None, *environment_names: str) -> str:
    if explicit:
        return explicit
    for name in environment_names:
        value = os.getenv(name)
        if value:
            return value
    names = ", ".join(environment_names)
    raise ValueError(f"API key is required; set one of: {names}")


__all__ = ["resolve_api_key"]
