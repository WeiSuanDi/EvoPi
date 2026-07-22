from evopi.policy.engine import PolicyEngine
from evopi.policy.registry import PolicyPack, PolicyRegistry
from evopi.policy.types import Policy


class PolicyManager:
    def __init__(self) -> None:
        self.registry = PolicyRegistry()
        self.engine = PolicyEngine(self.registry)

    def register(self, policy: Policy, *, replace: bool = False) -> None:
        self.registry.register(policy, replace=replace)

    def load_pack(self, pack: PolicyPack) -> None:
        self.registry.load_pack(pack)


__all__ = ["PolicyManager"]
