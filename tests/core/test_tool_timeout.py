"""Tests for Tool-level timeout via Tool.execute()."""

import asyncio

import pytest

from evopi.core.cancellation import AbortController
from evopi.core.tool import Tool, ToolResult


# ---------------------------------------------------------------------------
# Tool.timeout field validation
# ---------------------------------------------------------------------------

def test_tool_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        Tool(
            name="t",
            description="d",
            parameters={},
            handler=lambda: None,
            timeout=0,
        )


def test_tool_rejects_negative_grace_period() -> None:
    with pytest.raises(ValueError, match="timeout_grace_period cannot be negative"):
        Tool(
            name="t",
            description="d",
            parameters={},
            handler=lambda: None,
            timeout_grace_period=-0.1,
        )


def test_tool_defaults_timeout_to_none() -> None:
    tool = Tool(name="t", description="d", parameters={}, handler=lambda: None)
    assert tool.timeout is None
    assert tool.timeout_grace_period == 1.0


# ---------------------------------------------------------------------------
# Async handler timeout
# ---------------------------------------------------------------------------

def test_async_handler_completes_before_timeout() -> None:
    async def fast() -> str:
        return "done"

    tool = Tool(
        name="fast",
        description="d",
        parameters={},
        handler=fast,
        timeout=5.0,
    )
    result = asyncio.run(tool.execute({}))
    assert result == ToolResult(content="done")


def test_async_handler_times_out() -> None:
    async def slow() -> str:
        await asyncio.sleep(10)
        return "never"

    tool = Tool(
        name="slow",
        description="d",
        parameters={},
        handler=slow,
        timeout=0.05,
    )
    result = asyncio.run(tool.execute({}))
    assert result.is_error is True
    assert "timed out" in result.content
    assert result.metadata.get("timed_out") is True
    assert result.metadata.get("timeout") == 0.05


def test_async_handler_times_out_with_call_timeout_override() -> None:
    """Call-site timeout takes precedence over Tool.timeout."""
    async def slow() -> str:
        await asyncio.sleep(10)
        return "never"

    tool = Tool(
        name="slow",
        description="d",
        parameters={},
        handler=slow,
        timeout=5.0,  # would never fire in this test
    )
    result = asyncio.run(tool.execute({}, timeout=0.05))
    assert result.is_error is True
    assert "timed out" in result.content
    assert result.metadata.get("timeout") == 0.05


# ---------------------------------------------------------------------------
# Sync handler + timeout
# ---------------------------------------------------------------------------

def test_sync_handler_is_unaffected_by_timeout() -> None:
    """Sync handlers run inline; timeout is ignored for them."""
    tool = Tool(
        name="fast",
        description="d",
        parameters={},
        handler=lambda: "instant",
        timeout=60.0,
    )
    result = asyncio.run(tool.execute({}))
    assert result == ToolResult(content="instant")


# ---------------------------------------------------------------------------
# Timeout vs Abort interaction
# ---------------------------------------------------------------------------

def test_abort_wins_over_timeout() -> None:
    """When abort fires before timeout, abort result is returned."""
    async def slow() -> str:
        await asyncio.sleep(10)
        return "never"

    tool = Tool(name="slow", description="d", parameters={}, handler=slow, timeout=60.0)

    async def run() -> ToolResult:
        controller = AbortController(loop=asyncio.get_running_loop())
        # Abort almost immediately (before the 60s timeout)
        asyncio.get_running_loop().call_later(0.01, controller.abort)
        return await tool.execute({}, signal=controller.signal)

    result = asyncio.run(run())
    assert result.is_error is True
    assert result.metadata.get("aborted") is True


# ---------------------------------------------------------------------------
# Tool timeout grace period
# ---------------------------------------------------------------------------

def test_async_handler_gets_grace_period_on_timeout() -> None:
    """Cancelled handler is given timeout_grace_period to clean up."""
    cleaned_up = False

    async def handler() -> str:
        nonlocal cleaned_up
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)  # simulate cleanup
            cleaned_up = True
            raise
        return "never"

    tool = Tool(
        name="cleanup",
        description="d",
        parameters={},
        handler=handler,
        timeout=0.05,
        timeout_grace_period=1.0,
    )
    result = asyncio.run(tool.execute({}))
    assert result.is_error is True
    assert result.metadata.get("timed_out") is True
    # The handler should have had time to clean up during the grace period
    assert cleaned_up is True
