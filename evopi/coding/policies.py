from pathlib import Path

from evopi.policy.builtins import (
    FileWriteGuardPolicy,
    OutputTruncationPolicy,
    ShellSafetyPolicy,
    TestAfterEditPolicy,
    ToolConfirmationPolicy,
)
from evopi.policy.registry import PolicyPack


def coding_policy_pack(
    workspace: str | Path, *, max_output_chars: int = 20_000
) -> PolicyPack:
    plugins_dir = Path.home() / ".evopi" / "plugins"
    local_plugins = Path(workspace) / ".evopi" / "plugins"
    return PolicyPack(
        "coding",
        [
            ShellSafetyPolicy(),
            ToolConfirmationPolicy(tool_names={"shell_command"}),
            FileWriteGuardPolicy(
                workspace=Path(workspace),
                extra_allowed_dirs=(plugins_dir, local_plugins),
            ),
            OutputTruncationPolicy(max_chars=max_output_chars),
            TestAfterEditPolicy(),
        ],
    )


__all__ = ["coding_policy_pack"]
