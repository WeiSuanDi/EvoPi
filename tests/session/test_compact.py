"""Tests for token estimation, compaction triggers, and context assembly."""

from evopi.core.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from evopi.session.compact import (
    CompactionSettings,
    assemble_context,
    estimate_context_tokens,
    estimate_tokens,
    find_cut_point,
    should_compact,
)


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def test_estimate_user_message_tokens() -> None:
    msg = UserMessage(content="Hello, world!")
    assert estimate_tokens(msg) > 0


def test_estimate_assistant_message_with_tool_calls() -> None:
    from evopi.core.tool import ToolCall
    msg = AssistantMessage(
        content="Let me check that.",
        tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "/x/y"})],
        stop_reason="stop",
    )
    tokens = estimate_tokens(msg)
    assert tokens > 0


def test_estimate_system_message() -> None:
    msg = SystemMessage(content="You are a helpful assistant.")
    assert estimate_tokens(msg) > 0


def test_estimate_tool_result() -> None:
    msg = ToolResultMessage(
        content="file contents here",
        tool_call_id="c1",
        tool_name="read_file",
    )
    assert estimate_tokens(msg) > 0


def test_estimate_context_tokens_uses_usage_when_available() -> None:
    """When an assistant message has provider usage, it's used for estimation."""
    msgs = [
        UserMessage(content="Hi"),
        AssistantMessage(
            content="Hello",
            stop_reason="stop",
            metadata={"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
        ),
    ]
    tokens = estimate_context_tokens(msgs)
    assert tokens == 15  # usage.totalTokens + 0 trailing


def test_estimate_context_tokens_falls_back_to_heuristic() -> None:
    """Without provider usage, char/4 heuristic is used."""
    msgs = [
        UserMessage(content="Hi"),
        AssistantMessage(content="Hello", stop_reason="stop"),
    ]
    tokens = estimate_context_tokens(msgs)
    assert tokens > 0


def test_estimate_context_with_trailing_after_usage() -> None:
    """Trailing messages after usage are estimated heuristically."""
    msgs = [
        UserMessage(content="Hi"),
        AssistantMessage(
            content="Hello",
            stop_reason="stop",
            metadata={"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
        ),
        UserMessage(content="What else?"),
    ]
    tokens = estimate_context_tokens(msgs)
    # Should be >15 because trailing message adds tokens
    assert tokens >= 15


# ---------------------------------------------------------------------------
# Compaction trigger
# ---------------------------------------------------------------------------

def test_should_compact_when_below_threshold() -> None:
    settings = CompactionSettings(enabled=True, reserve_tokens=100, keep_recent_tokens=500)
    assert should_compact(1000, 1200, settings) is False


def test_should_compact_when_above_threshold() -> None:
    settings = CompactionSettings(enabled=True, reserve_tokens=100, keep_recent_tokens=500)
    assert should_compact(1150, 1200, settings) is True


def test_should_compact_disabled() -> None:
    settings = CompactionSettings(enabled=False)
    assert should_compact(9999, 10000, settings) is False


def test_should_compact_no_context_window() -> None:
    settings = CompactionSettings(enabled=True)
    assert should_compact(9999, 0, settings) is False


# ---------------------------------------------------------------------------
# Cut point detection
# ---------------------------------------------------------------------------

def test_find_cut_point_returns_valid_index() -> None:
    msgs = [
        UserMessage(content="Hello"),
        AssistantMessage(content="Hi there", stop_reason="stop"),
        UserMessage(content="Do something"),
        AssistantMessage(content="OK", stop_reason="stop"),
    ]
    cut = find_cut_point(msgs, keep_recent_tokens=1)
    assert 0 <= cut.first_kept_index <= len(msgs)


def test_find_cut_point_keeps_recent_messages() -> None:
    """The cut point should be near the end for a large keep_recent_tokens."""
    msgs = [
        UserMessage(content="Hello"),
        AssistantMessage(content="Hi there", stop_reason="stop"),
        UserMessage(content="Bye"),
        AssistantMessage(content="See you", stop_reason="stop"),
    ]
    cut = find_cut_point(msgs, keep_recent_tokens=99999)
    # Everything fits, should cut at start
    assert cut.first_kept_index <= 1


def test_find_cut_point_no_messages() -> None:
    cut = find_cut_point([], keep_recent_tokens=100)
    assert cut.first_kept_index == 0


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def test_assemble_context_without_compaction_is_identity() -> None:
    msgs = [
        UserMessage(content="Hello"),
        AssistantMessage(content="Hi", stop_reason="stop"),
    ]
    result = assemble_context(msgs)
    assert list(result) == msgs


def test_assemble_context_with_compaction_inserts_summary() -> None:
    msgs = [
        UserMessage(content="Hello"),
        AssistantMessage(content="Hi there!", stop_reason="stop"),
        UserMessage(content="What's the weather?"),
        AssistantMessage(content="It's sunny!", stop_reason="stop"),
    ]
    result = assemble_context(
        msgs,
        compact_summary="User said hello and asked about weather.",
        first_kept_index=2,
    )
    result_list = list(result)
    # First message is the compaction summary UserMessage
    assert result_list[0].role == "user"
    assert "summary" in result_list[0].content
    assert "User said hello" in result_list[0].content
    # Followed by messages from first_kept_index
    assert len(result_list) == 1 + (len(msgs) - 2)


# ---------------------------------------------------------------------------
# Default settings
# ---------------------------------------------------------------------------

def test_default_compaction_settings_are_enabled() -> None:
    from evopi.session.compact import DEFAULT_COMPACTION_SETTINGS
    assert DEFAULT_COMPACTION_SETTINGS.enabled is True
    assert DEFAULT_COMPACTION_SETTINGS.reserve_tokens == 16384
    assert DEFAULT_COMPACTION_SETTINGS.keep_recent_tokens == 20000
