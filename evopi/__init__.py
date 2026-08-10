"""EvoPi public package API."""

from importlib.metadata import PackageNotFoundError, version

from evopi.coding.harness import CodingHarness
from evopi.core.agent import Agent
from evopi.core.interaction import (
    InteractionContentError,
    InteractionContentTooLargeError,
    InteractionError,
    InteractionKind,
    InteractionLimits,
    InteractionModeError,
    InteractionOrigin,
    InteractionQueueClosedError,
    InteractionQueueFullError,
    InteractionQueueMode,
    InteractionQueueSnapshot,
    InteractionReceipt,
)
from evopi.harness import BaseHarness, ConfirmationBroker
from evopi.rpc import EventStream, HarnessRpcHost

try:
    __version__ = version("evopi")
except PackageNotFoundError:  # pragma: no cover - only an unpackaged source tree
    __version__ = "0.0.0+uninstalled"

__all__ = [
    "Agent",
    "BaseHarness",
    "CodingHarness",
    "ConfirmationBroker",
    "EventStream",
    "HarnessRpcHost",
    "InteractionContentError",
    "InteractionContentTooLargeError",
    "InteractionError",
    "InteractionKind",
    "InteractionLimits",
    "InteractionModeError",
    "InteractionOrigin",
    "InteractionQueueClosedError",
    "InteractionQueueFullError",
    "InteractionQueueMode",
    "InteractionQueueSnapshot",
    "InteractionReceipt",
    "__version__",
]
