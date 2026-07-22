"""Configuration shared by concrete AI adapters."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True, kw_only=True)
class ModelConfig:
    model: str
    base_url: str
    api_key: str
    max_tokens: int = 4096
    temperature: float | None = None
    timeout: float = 120.0
    headers: dict[str, str] = field(default_factory=dict)


__all__ = ["ModelConfig"]
