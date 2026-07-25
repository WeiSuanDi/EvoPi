"""Context token estimation, compaction triggers, and summary generation.

Mirrors Pi's compaction system: estimate tokens via char/4 heuristic, detect
overflow / threshold breach, find a valid cut point (with split-turn handling),
generate a structured summary via a model call (with incremental updates),
and assemble the compacted context for the Agent.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from evopi.core.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from evopi.core.model import Model

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token estimation (char / 4 heuristic, same as Pi)
# ---------------------------------------------------------------------------

_CHARS_PER_TOKEN = 4


def estimate_tokens(message: Message) -> int:
    """Estimate token count for one message using character-count heuristics."""
    chars = 0
    if isinstance(message, SystemMessage):
        chars = len(message.content)
    elif isinstance(message, UserMessage):
        chars = len(message.content)
    elif isinstance(message, AssistantMessage):
        chars = len(message.content)
        for tc in message.tool_calls:
            chars += len(tc.name) + len(str(tc.arguments))
    elif isinstance(message, ToolResultMessage):
        chars = len(message.content)
    else:
        chars = len(getattr(message, "content", "") or "")
    return max(1, (chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def estimate_context_tokens(
    messages: Sequence[Message],
    *,
    system_prompt: str = "",
    tools: Sequence[dict[str, Any]] | None = None,
) -> int:
    """Estimate total token count for a full model context.

    Includes *system_prompt* and tool definitions in addition to messages.
    Uses the latest assistant ``usage`` block when available, then adds a
    heuristic estimate for any trailing messages.
    """
    # Base estimate: system prompt + tools + messages
    base = _estimate_text(system_prompt)
    for tool in (tools or []):
        base += _estimate_text(str(tool))
    for m in messages:
        base += estimate_tokens(m)

    usage_tokens = _last_usage_tokens(messages)
    if usage_tokens is None:
        return base

    # Provider reported the exact token count for the context up to a point.
    # Use that + heuristic for everything after.
    found = False
    trailing = 0
    for m in messages:
        if found:
            trailing += estimate_tokens(m)
        elif m is usage_tokens or _provided_usage(m, usage_tokens):
            found = True
    return usage_tokens.totalTokens + trailing + _estimate_text(system_prompt) + sum(
        _estimate_text(str(t)) for t in (tools or [])
    )


def _estimate_text(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Compaction settings
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class CompactionSettings:
    enabled: bool = True
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000


DEFAULT_COMPACTION_SETTINGS = CompactionSettings()


# ---------------------------------------------------------------------------
# Threshold check
# ---------------------------------------------------------------------------


def should_compact(
    context_tokens: int,
    context_window: int,
    settings: CompactionSettings | None = None,
) -> bool:
    """Return True when context usage exceeds the compaction threshold."""
    if settings is None:
        settings = DEFAULT_COMPACTION_SETTINGS
    if not settings.enabled or context_window <= 0:
        return False
    return context_tokens > context_window - settings.reserve_tokens


# ---------------------------------------------------------------------------
# Cut-point detection (with split-turn handling like Pi)
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class CutPoint:
    first_kept_index: int
    turn_start_index: int = -1
    is_split_turn: bool = False


def find_cut_point(
    messages: Sequence[Message],
    keep_recent_tokens: int,
    *,
    start_index: int = 0,
) -> CutPoint:
    """Walk backwards from the newest message, accumulating estimated tokens
    until ``keep_recent_tokens`` is reached.

    Valid cut positions are user and assistant messages — tool results are
    skipped because a cut inside a turn's tool-execution phase would leave
    orphaned tool results without their triggering assistant message.

    When the cut lands on a non-user message (e.g. assistant), the method
    attempts to find the user message that started the turn so the caller
    can separately summarise the turn prefix (split-turn handling).
    """
    end_index = len(messages)
    # Collect valid cut positions: user and assistant (NOT tool_result)
    cut_indices: list[int] = []
    for i in range(start_index, end_index):
        msg = messages[i]
        if isinstance(msg, (UserMessage, AssistantMessage)):
            cut_indices.append(i)

    if not cut_indices:
        return CutPoint(first_kept_index=start_index)

    accumulated = 0
    cut_index = cut_indices[0]
    for i in range(end_index - 1, start_index - 1, -1):
        accumulated += estimate_tokens(messages[i])
        if accumulated >= keep_recent_tokens:
            for c in cut_indices:
                if c >= i:
                    cut_index = c
                    break
            break

    # Detect split turn
    cut_msg = messages[cut_index]
    is_user_cut = isinstance(cut_msg, UserMessage)
    turn_start_index = -1
    if not is_user_cut:
        turn_start_index = _find_turn_start(messages, cut_index, start_index)

    return CutPoint(
        first_kept_index=cut_index,
        turn_start_index=turn_start_index,
        is_split_turn=(not is_user_cut and turn_start_index >= 0),
    )


def _find_turn_start(
    messages: Sequence[Message],
    cut_index: int,
    start_index: int,
) -> int:
    """Walk backwards from *cut_index* to find the user message that started
    the turn containing the cut point."""
    for i in range(cut_index, start_index - 1, -1):
        if isinstance(messages[i], UserMessage):
            return i
    return -1


# ---------------------------------------------------------------------------
# Context assembly (mirrors Pi's defaultContextEntryTransform)
# ---------------------------------------------------------------------------


def assemble_context(
    messages: Sequence[Message],
    compact_summary: str | None = None,
    first_kept_index: int = 0,
) -> Sequence[Message]:
    """Build the message list sent to the model.

    When *compact_summary* is provided the compacted history is replaced by a
    single synthetic user message containing the summary.  Messages from
    *first_kept_index* onward are kept as-is.
    """
    if compact_summary is None:
        return list(messages)

    result: list[Message] = []
    result.append(
        UserMessage(
            content=(
                "<summary>\n"
                f"{compact_summary}\n"
                "</summary>\n\n"
                "The above is a summary of the earlier conversation. "
                "Continue helping based on this context and the recent messages below."
            ),
            metadata={"compaction_summary": True},
        )
    )
    for msg in messages[first_kept_index:]:
        result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Summary generation prompts (mirrors Pi's structured format)
# ---------------------------------------------------------------------------

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a "
    "conversation between a user and an AI assistant, then produce a "
    "structured summary following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions "
    "in the conversation. ONLY output the structured summary."
)

SUMMARIZATION_USER_PROMPT = """The conversation above is a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish?]

## Progress
### Done
- [x] [Completed tasks]

### In Progress
- [ ] [Current work]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [File paths, function names, error messages, or other details needed to continue]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

UPDATE_SUMMARIZATION_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

TURN_PREFIX_SUMMARIZATION_PROMPT = """This is the PREFIX of a turn that was too large to keep. The SUFFIX (recent work) is retained.

Summarize the prefix to provide context for the retained suffix:

## Original Request
[What did the user ask for in this turn?]

## Early Progress
- [Key decisions and work done in the prefix]

## Context for Suffix
- [Information needed to understand the retained recent work]

Be concise. Focus on what's needed to understand the kept suffix."""


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------


async def generate_summary(
    messages: Sequence[Message],
    model: Model,
    *,
    previous_summary: str | None = None,
) -> str:
    """Generate a structured compaction summary by calling the model.

    When *previous_summary* is provided the model is asked to merge new
    information into the existing summary rather than starting from scratch.
    """
    conversation_text = _serialize_conversation(messages)
    base_prompt = (
        UPDATE_SUMMARIZATION_PROMPT if previous_summary else SUMMARIZATION_USER_PROMPT
    )
    prompt_text = f"<conversation>\n{conversation_text}\n</conversation>\n\n{base_prompt}"
    if previous_summary:
        prompt_text = (
            f"<conversation>\n{conversation_text}\n</conversation>\n\n"
            f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
            f"{base_prompt}"
        )

    return await _call_model_for_summary(model, prompt_text)


async def generate_turn_prefix_summary(
    messages: Sequence[Message],
    model: Model,
) -> str:
    """Generate a compact summary for a split-turn prefix."""
    conversation_text = _serialize_conversation(messages)
    prompt_text = (
        f"<conversation>\n{conversation_text}\n</conversation>\n\n"
        f"{TURN_PREFIX_SUMMARIZATION_PROMPT}"
    )
    return await _call_model_for_summary(model, prompt_text)


async def _call_model_for_summary(model: Model, prompt_text: str) -> str:
    """Run a single-turn model call for summarization."""
    from evopi.core.context import AgentContext
    from evopi.core.stream import (
        AssistantMessageBuilder,
        ModelComplete,
        ModelStreamEvent,
        TextDelta,
    )

    context = AgentContext(
        messages=[
            SystemMessage(content=SUMMARIZATION_SYSTEM_PROMPT),
            UserMessage(content=prompt_text),
        ],
        tools=[],
    )

    builder = AssistantMessageBuilder()
    try:
        async for event in model.stream(context):  # type: ignore[arg-type]
            model_event: ModelStreamEvent = event  # type: ignore[assignment]
            if isinstance(model_event, TextDelta):
                builder.add_text(model_event.delta)
            elif isinstance(model_event, ModelComplete):
                return model_event.message.content.strip()
    except Exception as exc:
        _logger.exception("Summarization model call failed")
        raise CompactionError(f"Summarization failed: {exc}") from exc

    return builder.build(stop_reason="stop").content.strip()


# ---------------------------------------------------------------------------
# Full compaction orchestrator (split-turn + incremental)
# ---------------------------------------------------------------------------


async def compact_session(
    messages: Sequence[Message],
    model: Model,
    settings: CompactionSettings,
    *,
    previous_summary: str | None = None,
) -> tuple[str, CutPoint]:
    """Run a complete compaction cycle.

    1. Find the cut point (with split-turn detection).
    2. Summarise history (messages before the cut).
    3. If split-turn, separately summarise the turn prefix and merge.
    4. If *previous_summary* is provided, merge new info into it.
    5. Return the final summary and cut point.
    """
    cut = find_cut_point(messages, settings.keep_recent_tokens)

    if cut.is_split_turn and cut.turn_start_index >= 0:
        # Split turn: history up to turn start, prefix between turn start and cut
        history_msgs = list(messages[: cut.turn_start_index])
        prefix_msgs = list(messages[cut.turn_start_index : cut.first_kept_index])

        if history_msgs:
            history_summary = await generate_summary(
                history_msgs, model, previous_summary=previous_summary
            )
        else:
            history_summary = previous_summary or ""

        if prefix_msgs:
            prefix_summary = await generate_turn_prefix_summary(prefix_msgs, model)
            if history_summary:
                summary = (
                    f"{history_summary}\n\n---\n\n"
                    f"**Turn Context (split turn):**\n\n{prefix_summary}"
                )
            else:
                summary = prefix_summary
        else:
            summary = history_summary
    else:
        # Clean cut: summarise everything before the cut point
        history_msgs = list(messages[: cut.first_kept_index])
        if len(history_msgs) < 2:
            raise CompactionError("Nothing to compact")
        summary = await generate_summary(
            history_msgs, model, previous_summary=previous_summary
        )

    return summary, cut


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CompactionError(RuntimeError):
    """Raised when compaction fails (model error, aborted, etc.)."""


class _Usage:
    def __init__(self, data: dict[str, Any]) -> None:
        self.input: int = data.get("input_tokens", 0)
        self.output: int = data.get("output_tokens", 0)
        self.totalTokens: int = data.get("total_tokens", self.input + self.output)


def _last_usage_tokens(messages: Sequence[Message]) -> _Usage | None:
    for m in reversed(messages):
        if isinstance(m, AssistantMessage):
            usage_data = m.metadata.get("usage")
            if isinstance(usage_data, dict):
                usage = _Usage(usage_data)
                if usage.totalTokens > 0 and m.stop_reason not in ("aborted", "error"):
                    return usage
    return None


def _provided_usage(msg: Message, usage: _Usage) -> bool:
    if not isinstance(msg, AssistantMessage):
        return False
    data = msg.metadata.get("usage")
    if not isinstance(data, dict):
        return False
    u = _Usage(data)
    return u.totalTokens == usage.totalTokens and u.input == usage.input


def _serialize_conversation(messages: Sequence[Message]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = getattr(msg, "role", "?")
        content = getattr(msg, "content", str(msg))
        parts.append(f"[{role}]: {content}")
    return "\n\n".join(parts)


__all__ = [
    "CompactionError",
    "CompactionSettings",
    "CutPoint",
    "DEFAULT_COMPACTION_SETTINGS",
    "assemble_context",
    "compact_session",
    "estimate_context_tokens",
    "estimate_tokens",
    "find_cut_point",
    "generate_summary",
    "generate_turn_prefix_summary",
    "should_compact",
]
