from pathlib import Path

from evopi.policy.builtins import (
    FileWriteGuardPolicy,
    OutputTruncationPolicy,
    ShellSafetyPolicy,
    TestAfterEditPolicy,
)
from evopi.policy.registry import PolicyPack


def coding_policy_pack(
    workspace: str | Path, *, max_output_chars: int = 20_000
) -> PolicyPack:
    return PolicyPack(
        "coding",
        [
            ShellSafetyPolicy(),
            FileWriteGuardPolicy(workspace=Path(workspace)),
            OutputTruncationPolicy(max_chars=max_output_chars),
            TestAfterEditPolicy(),
        ],
    )


__all__ = ["coding_policy_pack"]
