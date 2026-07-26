"""EvoPi SubAgents — multi-agent collaboration with constrained scope.

Sub-agents are lightweight nested agents that run with a limited set of
tools and messages, returning validated results to the parent.  The parent
Harness governs sub-agent spawning via Policy hooks.
"""

from evopi.subagents.context_scope import GovernanceEnvelope, SubAgentScope
from evopi.subagents.manager import SubAgentError, SubAgentManager
from evopi.subagents.result_validation import SubAgentResult, validate_subagent_result

__all__ = [
    "GovernanceEnvelope",
    "SubAgentError",
    "SubAgentManager",
    "SubAgentResult",
    "SubAgentScope",
    "validate_subagent_result",
]
