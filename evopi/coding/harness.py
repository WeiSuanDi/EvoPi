"""The first domain Harness shipped with EvoPi."""

from __future__ import annotations

from pathlib import Path

from evopi.coding.policies import coding_policy_pack
from evopi.coding.prompts import CODING_SYSTEM_PROMPT
from evopi.coding.tools import coding_tools
from evopi.core.model import Model
from evopi.core.model_errors import ModelRetryConfig
from evopi.harness.base import BaseHarness
from evopi.harness.confirmation import ConfirmationHandler
from evopi.session import SessionManager


class CodingHarness(BaseHarness):
    def __init__(
        self,
        *,
        model: Model,
        workspace: str | Path,
        trace_path: str | Path | None = None,
        max_turns: int = 20,
        retry_config: ModelRetryConfig | None = None,
        max_output_chars: int = 20_000,
        system_prompt: str = CODING_SYSTEM_PROMPT,
        confirmation_handler: ConfirmationHandler | None = None,
        session_manager: SessionManager | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        super().__init__(
            model=model,
            system_prompt=system_prompt,
            trace_path=trace_path,
            max_turns=max_turns,
            retry_config=retry_config,
            confirmation_handler=confirmation_handler,
            session_manager=(
                session_manager or SessionManager.in_memory(self.workspace)
            ),
        )
        for tool in coding_tools(self.workspace):
            self.register_tool(tool)
        self.load_policy_pack(
            coding_policy_pack(self.workspace, max_output_chars=max_output_chars)
        )


__all__ = ["CodingHarness"]
