"""Registry for named Model instances."""

from __future__ import annotations

from evopi.core.model import Model


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, Model] = {}

    def register(self, name: str, model: Model, *, replace: bool = False) -> None:
        if name in self._models and not replace:
            raise ValueError(f"Model '{name}' is already registered")
        self._models[name] = model

    def get(self, name: str) -> Model:
        try:
            return self._models[name]
        except KeyError as exc:
            raise KeyError(f"Model '{name}' is not registered") from exc


__all__ = ["ModelRegistry"]
