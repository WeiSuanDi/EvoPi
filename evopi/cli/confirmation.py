"""Terminal adapter for EvoPi human confirmation requests."""

from __future__ import annotations

import json
from collections.abc import Callable

from evopi.harness.confirmation import ConfirmationRequest, ConfirmationResponse

InputFunction = Callable[[str], str]
OutputFunction = Callable[[str], None]


def terminal_confirmation_handler(
    request: ConfirmationRequest,
    *,
    input_fn: InputFunction | None = None,
    output_fn: OutputFunction | None = None,
) -> ConfirmationResponse:
    """Render one confirmation request and collect a conservative y/N decision."""

    read = input_fn or input
    write = output_fn or print
    arguments = request.arguments
    if arguments is None and request.tool_call is not None:
        arguments = request.tool_call.arguments

    write("")
    write("EvoPi requires confirmation")
    if request.tool_call is not None:
        write(f"Tool: {request.tool_call.name}")
    write(f"Risk: {request.risk_level}")
    write(f"Reason: {request.reason}")
    if arguments is not None:
        write("Arguments:")
        write(json.dumps(arguments, ensure_ascii=False, indent=2))

    try:
        choice = read("Approve? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = ""

    approved = choice in {"y", "yes"}
    return ConfirmationResponse(
        request_id=request.id,
        decision="approve" if approved else "deny",
        reason="Approved by user" if approved else "Denied by user",
        metadata={"interface": "cli"},
    )


__all__ = ["terminal_confirmation_handler"]
