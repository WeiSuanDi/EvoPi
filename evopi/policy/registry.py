"""Policy registration, enablement and hook lookup."""

from __future__ import annotations

from collections.abc import Iterable

from evopi.policy.types import HookName, Policy


class PolicyRegistry:
    def __init__(self, policies: Iterable[Policy] = ()) -> None:
        self._policies: dict[str, Policy] = {}
        for policy in policies:
            self.register(policy)

    def register(self, policy: Policy, *, replace: bool = False) -> None:
        if policy.name in self._policies and not replace:
            raise ValueError(f"Policy '{policy.name}' is already registered")
        self._policies[policy.name] = policy

    def get(self, name: str) -> Policy:
        try:
            return self._policies[name]
        except KeyError as exc:
            raise KeyError(f"Policy '{name}' is not registered") from exc

    def for_hook(self, hook: HookName) -> list[Policy]:
        policies = [
            policy
            for policy in self._policies.values()
            if policy.enabled and hook in policy.hooks
        ]
        return sorted(policies, key=lambda policy: (-policy.priority, policy.name))

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.get(name).enabled = enabled

    def load_pack(self, pack: "PolicyPack") -> None:
        for policy in pack.policies:
            self.register(policy, replace=True)


class PolicyPack:
    def __init__(self, name: str, policies: Iterable[Policy], *, version: str = "1.0.0") -> None:
        self.name = name
        self.version = version
        self.policies = list(policies)


__all__ = ["PolicyPack", "PolicyRegistry"]
