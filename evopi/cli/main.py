"""Command-line entry point for the EvoPi coding agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer
from prompt_toolkit.input import Input
from prompt_toolkit.output import Output
from rich.console import Console

from evopi.ai.models import model_from_config
from evopi.coding.harness import CodingHarness
from evopi.cli.confirmation import async_terminal_confirmation_handler
from evopi.cli.diagnostics import config_show_main, doctor_main
from evopi.cli.model_configuration import (
    IncompleteModelConfigurationError,
    resolve_cli_model_configuration,
)
from evopi.cli.display import ReplDisplay
from evopi.cli.policy_review import policy_review_main
from evopi.cli.policy_generation import policy_generate_main
from evopi.cli.policy import policy_init_main, policy_lifecycle_main
from evopi.cli.policy_discovery import policy_discover_main
from evopi.cli.plugin import plugin_main
from evopi.cli.product import (
    build_product_parser,
    build_run_result,
    compose_run_prompt,
    format_management_help,
    resolve_interaction_modes,
    run_exit_code,
)
from evopi.cli.resume import pick_session
from evopi.cli.rpc import run_stdio_rpc
from evopi.cli.repl import (
    ReplCommandContext,
    ReplCommandRegistry,
    ReplCompleter,
    ReplInputPreempted,
    ReplRunner,
    build_repl_startup_config,
    startup_panel,
)
from evopi.cli.runtime import (
    build_model_runtime,
    fallback_values_from_args,
    parse_tool_selection,
)
from evopi.cli.session import (
    print_session_opened,
    session_gc_main,
    session_list_main,
)
from evopi.cli.setup import SetupOptions, run_setup, setup_main
from evopi.cli.update import update_main
from evopi.core.events import CoreEvent
from evopi.core.model import Model
from evopi.core.model_errors import ModelRetryConfig
from evopi.harness import (
    ConfirmationBroker,
    ConfirmationHandler,
    ConfirmationResponse,
    InMemoryConfirmationStore,
)
from evopi.rpc import RpcError
from evopi.session import SessionManager
from evopi.tools import resolve_shell_environment

if TYPE_CHECKING:
    from evopi.evolution import PolicyActivationService


_UNREVIEWED_PLUGIN_WARNING = (
    "--plugin is a deprecated, unreviewed development override; "
    "use plugin review -> approve -> reload for product use"
)
_DEFAULT_CONFIRMATION_HANDLER = object()
_ModalResult = TypeVar("_ModalResult")


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _shell_mode(value: str) -> str:
    if value not in {"auto", "cmd", "powershell"}:
        raise argparse.ArgumentTypeError(
            "must be one of: auto, cmd, powershell"
        )
    return value


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evopi",
        description="Run the EvoPi coding agent",
    )
    parser.add_argument("prompt", nargs="?", help="Task for the agent")
    model_group = parser.add_argument_group("Model")
    model_group.add_argument(
        "--provider",
        choices=["anthropic", "openai-compatible", "openai-responses"],
    )
    model_group.add_argument("--model", help="Override the model name from .env")
    model_group.add_argument("--base-url", help="Override the provider Base URL")
    model_group.add_argument("--no-retry", action="store_true")
    model_group.add_argument("--max-retries", type=_non_negative_int, default=3)
    model_group.add_argument(
        "--max-output-tokens",
        type=_positive_int,
        default=4096,
        metavar="N",
        help="Maximum output tokens for each model attempt (default: 4096)",
    )
    model_group.add_argument(
        "--model-timeout", type=_positive_float, default=120.0,
        help="HTTP stream idle timeout in seconds",
    )
    model_group.add_argument(
        "--context-window", type=int, metavar="N",
        help="Model context window size for compaction decisions",
    )
    model_group.add_argument(
        "--fallback",
        action="append",
        metavar="PROVIDER:MODEL",
        help="Add an ordered failover candidate (repeatable)",
    )
    model_group.add_argument(
        "--no-failover",
        action="store_true",
        help="Disable model failover and reject configured fallbacks",
    )
    model_group.add_argument(
        "--circuit-failure-threshold",
        type=_positive_int,
        default=2,
        metavar="N",
        help="Failures before a candidate circuit opens (default: 2)",
    )
    model_group.add_argument(
        "--circuit-recovery-timeout",
        type=_non_negative_float,
        default=30.0,
        metavar="SECONDS",
        help="Circuit cooldown before a half-open probe (default: 30)",
    )

    runtime_group = parser.add_argument_group("Runtime")
    runtime_group.add_argument("--workspace", type=Path, default=Path.cwd())
    runtime_group.add_argument("--trace", type=Path, default=Path(".evopi/trace.jsonl"))
    runtime_group.add_argument(
        "--max-turns",
        type=_positive_int,
        default=os.getenv("EVOPI_MAX_TURNS", "20"),
        metavar="N",
        help="Strict model Turn budget, including the final answer Turn (default: 20)",
    )
    runtime_group.add_argument(
        "--shell",
        type=_shell_mode,
        choices=["auto", "cmd", "powershell"],
        default=os.getenv("EVOPI_SHELL", "auto"),
        help="Shell environment for shell_command (default: auto)",
    )
    runtime_group.add_argument(
        "--deadline", type=_positive_float, metavar="SECONDS",
        help="Wall-clock deadline for the entire run",
    )
    runtime_group.add_argument(
        "--tool-timeout", type=_positive_float, metavar="SECONDS",
        help="Default timeout for individual tool executions",
    )
    tool_selection = runtime_group.add_mutually_exclusive_group()
    tool_selection.add_argument(
        "--tools",
        metavar="NAME,...",
        help="Restrict the runtime to exactly these registered Tools",
    )
    tool_selection.add_argument(
        "--exclude-tools",
        metavar="NAME,...",
        help="Disable these registered Tools",
    )

    interactions_group = parser.add_argument_group("Interactions")
    interactions_group.add_argument(
        "--steering-mode",
        choices=["one-at-a-time", "all"],
        default=None,
        help=(
            "Queue mode for steering input while a Run is active "
            "(default: EVOPI_STEERING_MODE or one-at-a-time)"
        ),
    )
    interactions_group.add_argument(
        "--follow-up-mode",
        choices=["one-at-a-time", "all"],
        default=None,
        help=(
            "Queue mode for follow-up input at a terminal candidate "
            "(default: EVOPI_FOLLOW_UP_MODE or one-at-a-time)"
        ),
    )

    governance_group = parser.add_argument_group("Governance")
    governance_group.add_argument(
        "--approvals-path", type=Path, metavar="PATH",
        help="Path to the approvals JSON file",
    )
    governance_group.add_argument(
        "--approval-mode", choices=["strict", "warn", "off"], default="warn",
        help="Activation Gate mode (default: warn)",
    )
    governance_group.add_argument(
        "--no-evolved-policies",
        action="store_true",
        help="Do not load the current user's explicitly active evolved Policies",
    )

    session_options = parser.add_argument_group("Session")
    session_options.add_argument(
        "--compaction", choices=["on", "off"], default="on",
        help="Enable or disable automatic context compaction (default: on)",
    )
    session_group = session_options.add_mutually_exclusive_group()
    session_group.add_argument(
        "--new-session",
        action="store_true",
        help="Start a new persisted Session",
    )
    session_group.add_argument(
        "--session",
        metavar="ID|PATH",
        help="Open one persisted Session by UUID or path",
    )
    session_group.add_argument(
        "--no-session",
        action="store_true",
        help="Use an ephemeral in-memory Session",
    )
    session_group.add_argument(
        "--resume",
        action="store_true",
        help="Interactively pick a session to resume",
    )
    session_options.add_argument(
        "--session-root",
        type=Path,
        help="Override the persisted Session root directory",
    )

    resources_group = parser.add_argument_group("Resources")
    resources_group.add_argument(
        "--plugin", type=Path, action="append", metavar="PATH",
        help="Load an unreviewed development plugin from PATH (deprecated)",
    )
    memory_group = resources_group.add_mutually_exclusive_group()
    memory_group.add_argument(
        "--memory", type=Path, metavar="PATH",
        help="Enable persistent memory (JSON file path, e.g. .evopi/memory.json)",
    )
    memory_group.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable workspace Memory for this invocation",
    )
    resources_group.add_argument(
        "--skills-root", type=Path, metavar="PATH",
        help="Enable skill loading from PATH (e.g. ~/.evopi/skills)",
    )
    resources_group.add_argument(
        "--enable-subagent", action="store_true",
        help="Enable the spawn_subagent tool for delegated tasks",
    )

    advanced_group = parser.add_argument_group("Advanced")
    advanced_group.add_argument(
        "--system-prompt", metavar="TEXT",
        help="Override the default system prompt",
    )
    advanced_group.add_argument(
        "--append-system-prompt",
        metavar="TEXT",
        help="Append instructions after the generated or replacement system prompt",
    )
    return parser


def _build_harness(
    args: argparse.Namespace,
    *,
    confirmation_handler: object = _DEFAULT_CONFIRMATION_HANDLER,
    confirmation_broker: ConfirmationBroker | None = None,
) -> CodingHarness:
    """Build a CodingHarness from CLI args, auto-detecting available modules."""
    from evopi.session.compact import CompactionSettings

    shell_environment = resolve_shell_environment(
        getattr(args, "shell", os.getenv("EVOPI_SHELL", "auto"))
    )
    model_options: dict[str, Any] = {
        "timeout": getattr(args, "model_timeout", 120.0),
        "model": getattr(args, "model", None),
        "context_window": getattr(args, "context_window", 0),
    }
    if hasattr(args, "max_output_tokens"):
        model_options["max_tokens"] = args.max_output_tokens
    if getattr(args, "base_url", None) is not None:
        model_options["base_url"] = args.base_url
    if getattr(args, "model_profile", None) is not None:
        model_options["profile_name"] = args.model_profile
    fallback_values = fallback_values_from_args(args)
    if fallback_values or getattr(args, "no_failover", False):
        model, model_route, _ = build_model_runtime(args)
    else:
        model = model_from_environment(
            getattr(args, "provider", None),
            **model_options,
        )
        model_route = None
    included_tools, excluded_tools = parse_tool_selection(args)
    session_manager = _session_manager_from_args(args)
    resolved_confirmation_handler = (
        async_terminal_confirmation_handler
        if confirmation_handler is _DEFAULT_CONFIRMATION_HANDLER
        else cast(ConfirmationHandler | None, confirmation_handler)
    )

    # Auto-detect Memory — always on, stored in workspace
    memory_path = getattr(args, "memory", None)
    if getattr(args, "no_memory", False):
        memory_path = None
    elif memory_path is None:
        default_memory = args.workspace / ".evopi" / "memory.json"
        memory_path = default_memory

    # Auto-detect Skills — on if skills directory exists
    skills_root, resource_warnings = _skills_root_from_args(args)
    if getattr(args, "plugin", None):
        resource_warnings = (*resource_warnings, _UNREVIEWED_PLUGIN_WARNING)

    # SubAgent — explicit opt-in only
    enable_subagent = getattr(args, "enable_subagent", False)
    steering_mode, follow_up_mode = resolve_interaction_modes(args)

    return CodingHarness(
        model=model,
        model_route=model_route,
        workspace=args.workspace,
        trace_path=args.trace,
        max_turns=getattr(args, "max_turns", 20),
        system_prompt=getattr(args, "system_prompt", None),
        append_system_prompt=getattr(args, "append_system_prompt", None),
        retry_config=ModelRetryConfig(
            enabled=not getattr(args, "no_retry", False),
            max_retries=getattr(args, "max_retries", 3),
        ),
        confirmation_handler=resolved_confirmation_handler,
        confirmation_broker=confirmation_broker,
        session_manager=session_manager,
        deadline=getattr(args, "deadline", None),
        tool_timeout=getattr(args, "tool_timeout", None),
        approvals_path=getattr(args, "approvals_path", None),
        approval_mode=getattr(args, "approval_mode", "warn"),
        compaction_settings=CompactionSettings(
            enabled=(getattr(args, "compaction", "on") == "on"),
        ),
        plugin_paths=_plugin_paths_from_args(args),
        memory_path=memory_path,
        skills_root=skills_root,
        enable_subagent=enable_subagent,
        tool_names=included_tools,
        excluded_tool_names=excluded_tools,
        reserved_plugin_commands=ReplCommandRegistry().reserved_plugin_commands,
        resource_warnings=resource_warnings,
        policy_activation_service=_policy_activation_service_from_args(args),
        shell_environment=shell_environment,
        steering_mode=steering_mode,
        follow_up_mode=follow_up_mode,
    )


def _policy_activation_service_from_args(
    args: argparse.Namespace,
) -> PolicyActivationService | None:
    if getattr(args, "no_evolved_policies", False):
        return None
    from evopi.evolution import (
        ActivationStore,
        PolicyActivationService,
        PolicyArtifactStore,
        PolicySelectionStore,
        resolve_evolution_home,
    )

    home = resolve_evolution_home()
    return PolicyActivationService(
        ActivationStore(home / "activations.json"),
        PolicyArtifactStore(home / "artifacts" / "policies"),
        PolicySelectionStore(home / "policy-selections.json"),
    )


def model_from_environment(
    provider: str | None = None,
    *,
    timeout: float = 120.0,
    model: str | None = None,
    base_url: str | None = None,
    context_window: int = 0,
    max_tokens: int = 4096,
    profile_name: str | None = None,
) -> Model:
    """Compatibility seam backed by the CLI's persisted configuration resolver."""

    resolved = resolve_cli_model_configuration(
        provider,
        model=model,
        base_url=base_url,
        profile_name=profile_name,
        require_complete=True,
    )
    return model_from_config(
        resolved.safe,
        api_key=resolved.api_key,
        timeout=timeout,
        context_window=context_window,
        max_tokens=max_tokens,
    )

def _skills_root_from_args(
    args: argparse.Namespace,
) -> tuple[Path | None, tuple[str, ...]]:
    explicit = getattr(args, "skills_root", None)
    if explicit is not None:
        return explicit, ()
    from evopi.evolution import WorkspaceTrustStore
    from evopi.plugins import resolve_evopi_home

    workspace = Path(args.workspace).expanduser().resolve()
    project = workspace / ".evopi" / "skills"
    home = resolve_evopi_home()
    trust_path = home / "workspace-trust.json"
    trusted = (
        trust_path.exists()
        and WorkspaceTrustStore(trust_path).is_trusted(workspace)
    )
    if project.exists() and trusted:
        return project, ()
    warnings = (
        ("Project Skills were skipped because the workspace is not trusted",)
        if project.exists()
        else ()
    )
    global_root = home / "skills"
    return (global_root if global_root.exists() else None), warnings


def _create_repl_prompt_session(
    *,
    input: Input | None = None,
    output: Output | None = None,
    completer: Completer | None = None,
) -> PromptSession[str]:
    """Create the REPL editor without retaining its submitted input line."""

    return PromptSession[str](
        erase_when_done=True,
        input=input,
        output=output,
        completer=completer,
        complete_while_typing=False,
    )


def _plugin_paths_from_args(args: argparse.Namespace) -> list[str | Path]:
    from evopi.plugins import approved_plugin_entrypoints

    approved = list(approved_plugin_entrypoints(args.workspace))
    explicit = list(getattr(args, "plugin", None) or [])
    if explicit:
        print(
            f"EvoPi warning: {_UNREVIEWED_PLUGIN_WARNING}.",
            file=sys.stderr,
        )
    paths: list[str | Path] = [*approved, *explicit]
    return list(dict.fromkeys(paths))


async def _run_one_shot(
    args: argparse.Namespace,
    *,
    json_output: bool = False,
) -> int:
    """Single prompt → response → exit."""
    prompt_text = args.prompt
    harness: CodingHarness | None = None
    try:
        harness = _build_harness(args)

        def display(event: CoreEvent) -> None:
            if event.type == "session_start":
                print_session_opened(harness.session)  # type: ignore[arg-type]
            elif (
                not json_output
                and event.type == "message_update"
                and event.data.get("kind") == "text"
            ):
                print(event.data.get("delta", ""), end="", flush=True)
            elif event.type == "model_retry_start":
                info = event.data.get("error_info")
                kind = getattr(info, "kind", "unknown")
                print(
                    f"\nEvoPi retrying model after {kind} error "
                    f"(attempt {event.data.get('next_attempt')}, "
                    f"delay {event.data.get('delay')}s)...",
                    file=sys.stderr,
                    flush=True,
                )

        harness.subscribe(display)
        try:
            answer = await harness.prompt(prompt_text)
        except (ValueError, RuntimeError):
            if not json_output:
                raise
            payload = build_run_result(harness, None)
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            return run_exit_code(payload["end_reason"])
        if json_output:
            payload = build_run_result(harness, answer)
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            return run_exit_code(payload["end_reason"])
        if not answer.content.endswith("\n"):
            print()
        state = harness.agent.last_run
        return run_exit_code(state.end_reason if state is not None else "completed")
    finally:
        if harness is not None and not harness.is_running:
            harness.close()


class _TerminalEditor:
    """Preemptible single-owner terminal coordinator.

    Ordinary queue editing remains active while a Run streams. Confirmation
    and Plugin UI calls acquire modal ownership: they cancel the ordinary
    PromptToolkit read, wait for its cleanup, perform exactly one modal read,
    then let the REPL recreate its editor. No two terminal readers overlap.
    """

    def __init__(self) -> None:
        self._session: PromptSession[str] | None = None
        self._state_lock = asyncio.Lock()
        self._modal_lock = asyncio.Lock()
        self._modal_complete = asyncio.Event()
        self._modal_complete.set()
        self._active_read: asyncio.Task[str] | None = None
        self._preempted: set[asyncio.Task[str]] = set()

    def attach(self, session: PromptSession[str]) -> None:
        if self._session is not None:
            raise RuntimeError("Terminal editor already has a PromptSession")
        self._session = session

    def _require_session(self) -> PromptSession[str]:
        if self._session is None:
            raise RuntimeError("Terminal editor is not attached")
        return self._session

    async def read(self, label: str) -> str:
        session = self._require_session()
        while True:
            await self._modal_complete.wait()
            async with self._state_lock:
                if not self._modal_complete.is_set():
                    continue
                task = asyncio.create_task(session.prompt_async(label))
                self._active_read = task
            try:
                return await task
            except asyncio.CancelledError as exc:
                if task not in self._preempted:
                    raise
                raise ReplInputPreempted from exc
            finally:
                async with self._state_lock:
                    if self._active_read is task:
                        self._active_read = None
                    self._preempted.discard(task)

    async def modal_read(self, label: str) -> str:
        session = self._require_session()
        return await self.run_modal(lambda: session.prompt_async(label))

    async def run_modal(
        self,
        operation: Callable[[], Awaitable[_ModalResult]],
    ) -> _ModalResult:
        async with self._modal_lock:
            self._modal_complete.clear()
            async with self._state_lock:
                active = self._active_read
                if active is not None and not active.done():
                    self._preempted.add(active)
                    active.cancel()
            if active is not None:
                await asyncio.gather(active, return_exceptions=True)
            try:
                return await operation()
            finally:
                self._modal_complete.set()


def _gated_confirmation_handler(editor: _TerminalEditor) -> ConfirmationHandler:
    """Give Confirmation modal ownership over the active REPL editor."""

    async def handler(
        request: Any,
        *,
        signal: Any = None,
    ) -> ConfirmationResponse:
        async def operation() -> ConfirmationResponse:
            return await async_terminal_confirmation_handler(
                request,
                signal=signal,
            )

        return await editor.run_modal(operation)

    return handler


async def _run_repl(
    args: argparse.Namespace,
    *,
    initial_prompt: str | None = None,
) -> int:
    """Multi-turn REPL: one coordinated reader, concurrent Run and input."""
    console = Console(file=sys.stderr)
    editor = _TerminalEditor()
    harness = _build_harness(
        args,
        confirmation_handler=_gated_confirmation_handler(editor),
    )
    try:
        display = ReplDisplay()
        display.set_status(
            f"Model: {harness.model.name} | "
            f"Session: {harness.session.session_id[:12]}..."
        )
        harness.subscribe(display.handle_event)

        registry = ReplCommandRegistry()
        command_context = ReplCommandContext(
            harness=harness,
            startup=build_repl_startup_config(args, harness),
            display=display,
            console=console,
        )
        session = _create_repl_prompt_session(
            completer=ReplCompleter(
                registry=registry,
                context=command_context,
            )
        )
        editor.attach(session)
        console.print(startup_panel(command_context))
        for warning in harness.capabilities.warnings:
            console.print(f"[yellow]Warning: {warning}[/]")
        from evopi.cli.plugin_ui import ReplPluginUI

        harness.attach_plugin_ui(
            ReplPluginUI(
                display=display,
                prompt=editor.modal_read,
                console=console,
            )
        )
        runner = ReplRunner(
            harness=harness,
            display=display,
            console=console,
            registry=registry,
            context=command_context,
            read=editor.read,
            initial_prompt=initial_prompt,
        )
        return await runner.run()
    finally:
        if not harness.is_running:
            harness.close()


def _session_manager_from_args(args: argparse.Namespace) -> SessionManager:
    workspace = getattr(args, "workspace", Path.cwd())
    if not hasattr(args, "session_root"):
        return SessionManager.in_memory(workspace)
    root = args.session_root
    if getattr(args, "no_session", False):
        return SessionManager.in_memory(workspace)
    if getattr(args, "resume", False):
        return pick_session(workspace, root=root)
    if getattr(args, "new_session", False):
        return SessionManager.create(workspace, root=root)
    if getattr(args, "session", None) is not None:
        return SessionManager.open(args.session, workspace=workspace, root=root)
    return SessionManager.continue_recent(workspace, root=root)


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)

    if raw_args in (["--help"], ["-h"]):
        print(build_product_parser().format_help(), end="")
        return 0
    if raw_args == ["--version"]:
        from evopi import __version__

        print(f"EvoPi {__version__}")
        return 0
    if raw_args[:1] == ["setup"]:
        try:
            return setup_main(raw_args[1:])
        except KeyboardInterrupt:
            print("\nEvoPi setup aborted.", file=sys.stderr)
            return 130
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"EvoPi setup error: {exc}", file=sys.stderr)
            return 1
    if raw_args[:1] == ["update"]:
        try:
            return update_main(raw_args[1:])
        except KeyboardInterrupt:
            print("\nEvoPi update aborted.", file=sys.stderr)
            return 130
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"EvoPi update error: {exc}", file=sys.stderr)
            return 1
    if raw_args[:2] == ["remote", "serve"]:
        try:
            return _remote_serve_main(raw_args[2:])
        except KeyboardInterrupt:
            print("\nEvoPi Remote Gateway stopped.", file=sys.stderr)
            return 130
        except (OSError, ValueError, RuntimeError, RpcError) as exc:
            print(f"EvoPi remote error: {exc}", file=sys.stderr)
            return 1
    if raw_args[:1] == ["remote"]:
        try:
            from evopi.cli.remote import remote_main

            return remote_main(raw_args[1:])
        except KeyboardInterrupt:
            print("\nEvoPi remote command aborted.", file=sys.stderr)
            return 130
    if len(raw_args) == 2 and raw_args[0] in {"session", "policy", "plugin"} and raw_args[1] in {
        "--help",
        "-h",
    }:
        print(format_management_help(raw_args[0]))
        return 0
    if raw_args[:2] == ["policy", "review"]:
        return policy_review_main(raw_args[2:])
    if raw_args[:2] == ["policy", "init"]:
        return policy_init_main(raw_args[2:])
    if raw_args[:2] == ["policy", "generate"]:
        return policy_generate_main(raw_args[2:])
    if raw_args[:2] == ["policy", "discover"]:
        return policy_discover_main(raw_args[2:])
    if len(raw_args) >= 2 and raw_args[0] == "policy":
        return policy_lifecycle_main(raw_args[1], raw_args[2:])
    if raw_args[:2] == ["session", "list"]:
        return session_list_main(raw_args[2:])
    if raw_args[:2] == ["session", "gc"]:
        return session_gc_main(raw_args[2:])
    if len(raw_args) >= 2 and raw_args[0] == "plugin":
        return plugin_main(raw_args[1], raw_args[2:])
    if raw_args[:2] == ["config", "show"]:
        return config_show_main(raw_args[2:])
    if raw_args[:1] == ["config"]:
        print(format_management_help("config"))
        return 0 if raw_args[1:] in (["--help"], ["-h"]) else 2
    if raw_args[:1] == ["doctor"]:
        return doctor_main(raw_args[1:])

    try:
        if raw_args[:1] == ["rpc"]:
            rpc_parser = build_parser()
            rpc_parser.prog = "evopi rpc"
            rpc_parser.description = "Run the local EvoPi JSONL host over stdio"
            args = rpc_parser.parse_args(raw_args[1:])
            if getattr(args, "prompt", None) is not None:
                print("EvoPi rpc does not accept a positional prompt.", file=sys.stderr)
                return 2
            broker = ConfirmationBroker(InMemoryConfirmationStore())
            harness = _build_harness(
                args,
                confirmation_handler=None,
                confirmation_broker=broker,
            )
            return asyncio.run(run_stdio_rpc(harness, broker))
        if raw_args[:1] == ["chat"]:
            chat_parser = build_parser()
            chat_parser.prog = "evopi chat"
            args = chat_parser.parse_args(raw_args[1:])
            setup_result = _ensure_interactive_setup(args)
            if setup_result is not None:
                return setup_result
            return asyncio.run(
                _run_repl(args, initial_prompt=getattr(args, "prompt", None))
            )
        if raw_args[:1] == ["run"]:
            run_parser = build_parser()
            run_parser.prog = "evopi run"
            run_parser.add_argument(
                "--json",
                action="store_true",
                dest="json_output",
                help="Emit one stable JSON result to stdout",
            )
            args = run_parser.parse_args(raw_args[1:])
            stdin_text = _read_piped_stdin()
            args.prompt = compose_run_prompt(stdin_text, getattr(args, "prompt", None))
            if not args.prompt:
                print(
                    "EvoPi run requires a prompt or piped stdin.",
                    file=sys.stderr,
                )
                return 2
            return asyncio.run(
                _run_one_shot(args, json_output=bool(args.json_output))
            )

        args = (
            build_parser().parse_args()
            if argv is None
            else build_parser().parse_args(raw_args)
        )
        if getattr(args, "prompt", None):
            return asyncio.run(_run_one_shot(args))
        setup_result = _ensure_interactive_setup(args)
        if setup_result is not None:
            return setup_result
        return asyncio.run(_run_repl(args))
    except IncompleteModelConfigurationError as exc:
        print(f"EvoPi configuration error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, RpcError) as exc:
        print(f"EvoPi error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nEvoPi aborted.")
        return 130


def _remote_serve_main(argv: Sequence[str]) -> int:
    from evopi.cli.remote import build_remote_serve_parser
    from evopi.configuration import resolve_user_config_home
    from evopi.remote import (
        RemoteGatewayConfig,
        RemoteHostStore,
        ensure_tls_files,
        serve_remote_gateway,
    )

    remote_args, runtime_argv = build_remote_serve_parser().parse_known_args(list(argv))
    runtime_parser = build_parser()
    runtime_parser.prog = "evopi remote serve"
    runtime_args = runtime_parser.parse_args(runtime_argv)
    if runtime_args.prompt is not None:
        raise ValueError("Remote Gateway does not accept a positional prompt")
    root = remote_args.remote_root or resolve_user_config_home() / "remote"
    store = RemoteHostStore(root)
    host_config = store.load_config(remote_args.name)
    runtime_args.workspace = host_config.workspace
    runtime_args.model_profile = host_config.model_profile
    allowed_hosts = tuple(remote_args.allowed_hosts or ())
    if not allowed_hosts:
        if remote_args.bind in {"127.0.0.1", "::1"}:
            allowed_hosts = ("localhost", "127.0.0.1", "::1")
        else:
            raise ValueError("non-loopback Remote serve requires --allowed-host")
    gateway_config = RemoteGatewayConfig(
        bind=remote_args.bind,
        port=remote_args.port,
        proxy_mode=remote_args.proxy_mode,
        cert_file=remote_args.cert,
        key_file=remote_args.key,
        trusted_proxy_cidrs=tuple(
            remote_args.trusted_proxies or ("127.0.0.0/8", "::1/128")
        ),
        allowed_hosts=allowed_hosts,
        allowed_origins=tuple(remote_args.allowed_origins or ()),
        console_enabled=remote_args.console,
        max_connections=remote_args.max_connections,
        max_connections_per_ip=remote_args.max_connections_per_ip,
        max_connections_per_device=remote_args.max_connections_per_device,
        max_outbound_items=remote_args.max_outbound_items,
        max_outbound_bytes=remote_args.max_outbound_bytes,
        handshake_rate_per_minute=remote_args.handshake_rate,
        pairing_rate_per_minute=remote_args.pairing_rate,
        request_rate_per_minute=remote_args.request_rate,
        run_rate_per_minute=remote_args.run_rate,
        confirmation_rate_per_minute=remote_args.confirmation_rate,
    )
    ensure_tls_files(gateway_config)
    broker = ConfirmationBroker(InMemoryConfirmationStore())
    harness = _build_harness(
        runtime_args,
        confirmation_handler=None,
        confirmation_broker=broker,
    )
    return asyncio.run(
        serve_remote_gateway(
            harness,
            broker,
            store=store,
            host_name=remote_args.name,
            gateway_config=gateway_config,
        )
    )


def _read_piped_stdin() -> str:
    try:
        if sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    except (AttributeError, OSError):
        return ""


def _ensure_interactive_setup(args: argparse.Namespace) -> int | None:
    try:
        resolve_cli_model_configuration(
            getattr(args, "provider", None),
            model=getattr(args, "model", None),
            base_url=getattr(args, "base_url", None),
            require_complete=True,
        )
        return None
    except IncompleteModelConfigurationError:
        if not sys.stdin.isatty():
            print(
                "EvoPi model configuration is incomplete; run 'evopi setup'.",
                file=sys.stderr,
            )
            return 2
        result = run_setup(
            SetupOptions(
                provider=getattr(args, "provider", None),
                model=getattr(args, "model", None),
                base_url=getattr(args, "base_url", None),
            ),
            interactive=True,
        )
        return None if result == 0 else result


__all__ = [
    "build_parser",
    "build_product_parser",
    "build_run_result",
    "compose_run_prompt",
    "main",
    "run_exit_code",
]

if __name__ == "__main__":
    raise SystemExit(main())
