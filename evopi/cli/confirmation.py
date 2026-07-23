"""Terminal adapter for EvoPi human confirmation requests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import nullcontext, suppress

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from evopi.core.cancellation import AbortSignal
from evopi.harness.confirmation import ConfirmationRequest, ConfirmationResponse

InputFunction = Callable[[str], str]
OutputFunction = Callable[[str], None]


def _render_request(request: ConfirmationRequest, *, output_fn: OutputFunction) -> None:
    arguments = request.arguments
    if arguments is None and request.tool_call is not None:
        arguments = request.tool_call.arguments

    output_fn("")
    output_fn("EvoPi requires confirmation")
    if request.tool_call is not None:
        output_fn(f"Tool: {request.tool_call.name}")
    output_fn(f"Risk: {request.risk_level}")
    output_fn(f"Reason: {request.reason}")
    if arguments is not None:
        output_fn("Arguments:")
        output_fn(json.dumps(arguments, ensure_ascii=False, indent=2))


def _response(request: ConfirmationRequest, choice: str) -> ConfirmationResponse:
    approved = choice.strip().lower() in {"y", "yes"}
    return ConfirmationResponse(
        request_id=request.id,
        decision="approve" if approved else "deny",
        reason="Approved by user" if approved else "Denied by user",
        metadata={"interface": "cli"},
    )


def terminal_confirmation_handler(
    request: ConfirmationRequest,
    *,
    input_fn: InputFunction | None = None,
    output_fn: OutputFunction | None = None,
) -> ConfirmationResponse:
    """Render one confirmation request and collect a conservative y/N decision."""

    read = input_fn or input
    write = output_fn or print
    _render_request(request, output_fn=write)

    try:
        choice = read("Approve? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = ""

    return _response(request, choice)


async def async_terminal_confirmation_handler(
    request: ConfirmationRequest,
    *,
    signal: AbortSignal | None = None,
    session: PromptSession[str] | None = None,
    output_fn: OutputFunction | None = None,
) -> ConfirmationResponse:
    """Collect confirmation without blocking the event loop."""

    write = output_fn or print
    _render_request(request, output_fn=write)
    if signal is not None and signal.aborted:
        return ConfirmationResponse(
            request_id=request.id,
            decision="cancelled",
            reason="Run aborted while waiting for confirmation",
            metadata={"interface": "cli", "aborted": True},
        )

    owns_session = session is None
    prompt_session = session or PromptSession[str]()

    async def read_choice() -> tuple[str, bool]:
        try:
            return await prompt_session.prompt_async("Approve? [y/N]: "), False
        except KeyboardInterrupt:
            return "", True

    prompt_task: asyncio.Task[tuple[str, bool]] | None = None
    abort_task: asyncio.Task[None] | None = None
    try:
        stdout_context = patch_stdout() if owns_session else nullcontext()
        with stdout_context:
            prompt_task = asyncio.create_task(read_choice())
            if signal is None:
                choice, interrupted = await prompt_task
            else:
                abort_task = asyncio.create_task(signal.wait())
                done, _ = await asyncio.wait(
                    {prompt_task, abort_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if abort_task in done:
                    prompt_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await prompt_task
                    return ConfirmationResponse(
                        request_id=request.id,
                        decision="cancelled",
                        reason="Run aborted while waiting for confirmation",
                        metadata={"interface": "cli", "aborted": True},
                    )
                abort_task.cancel()
                with suppress(asyncio.CancelledError):
                    await abort_task
                choice, interrupted = await prompt_task
        if interrupted:
            return ConfirmationResponse(
                request_id=request.id,
                decision="cancelled",
                reason="Cancelled by user",
                metadata={"interface": "cli"},
            )
    except KeyboardInterrupt:
        return ConfirmationResponse(
            request_id=request.id,
            decision="cancelled",
            reason="Cancelled by user",
            metadata={"interface": "cli"},
        )
    except EOFError:
        choice = ""
    finally:
        if abort_task is not None and not abort_task.done():
            abort_task.cancel()
        if prompt_task is not None and not prompt_task.done():
            prompt_task.cancel()

    return _response(request, choice)


__all__ = ["async_terminal_confirmation_handler", "terminal_confirmation_handler"]
