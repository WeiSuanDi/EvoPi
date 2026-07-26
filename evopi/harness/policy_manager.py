from evopi.policy.approval import ApprovalStore
from evopi.policy.engine import PolicyEngine
from evopi.policy.registry import PolicyPack, PolicyRegistry
from evopi.policy.types import Policy


class PolicyManager:
    def __init__(self, approval_store: ApprovalStore | None = None) -> None:
        self.registry = PolicyRegistry()
        self.engine = PolicyEngine(self.registry)
        self.approval_store = approval_store or ApprovalStore(None, mode="off")

    def register(self, policy: Policy, *, replace: bool = False) -> None:
        loaded = self.approval_store.check_policy(policy)
        loaded.raise_if_required(policy.name, policy.version)
        self.registry.register(policy, replace=replace)

    def all(self) -> list[Policy]:
        return self.registry.all()

    def load_pack(self, pack: PolicyPack) -> None:
        for policy in pack.policies:
            loaded = self.approval_store.check_policy(policy)
            loaded.raise_if_required(policy.name, policy.version)
        self.registry.load_pack(pack)


__all__ = ["PolicyManager"]
