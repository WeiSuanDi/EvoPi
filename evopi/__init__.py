"""EvoPi public package API."""

from evopi.coding.harness import CodingHarness
from evopi.core.agent import Agent
from evopi.harness import BaseHarness, ConfirmationBroker
from evopi.rpc import EventStream, HarnessRpcHost

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "BaseHarness",
    "CodingHarness",
    "ConfirmationBroker",
    "EventStream",
    "HarnessRpcHost",
    "__version__",
]
