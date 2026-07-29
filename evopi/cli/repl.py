"""Registry-driven EvoPi REPL workbench."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from evopi.coding import CodingHarness

ReplCommandAction = Literal["continue", "retry", "quit"]


class ReplDisplayHost(Protocol):
    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def set_status(self, text: str) -> None: ...


@dataclass(slots=True, frozen=True, kw_only=True)
class ReplStartupConfig:
    provider: str
    model: str
    base_url: str
    workspace: str
    session_mode: str
    retry_enabled: bool
    max_retries: int
    deadline: float | None
    tool_timeout: float | None
    fallbacks: tuple[str, ...]
    included_tools: tuple[str, ...] | None
    excluded_tools: tuple[str, ...] | None
    credential_configured: bool = False


@dataclass(slots=True, kw_only=True)
class ReplCommandContext:
    harness: CodingHarness
    startup: ReplStartupConfig
    display: ReplDisplayHost | None
    console: Console
    recent_prompt: str | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class ReplCommandResult:
    action: ReplCommandAction = "continue"
    prompt: str | None = None
    exit_code: int = 0


ReplCommandHandler = Callable[
    [ReplCommandContext, str, str],
    ReplCommandResult | Awaitable[ReplCommandResult],
]


@dataclass(slots=True, frozen=True, kw_only=True)
class ReplCommandSpec:
    name: str
    usage: str
    description: str
    group: str
    handler: ReplCommandHandler
    source: str = "builtin"


def build_repl_startup_config(
    args: Any,
    harness: CodingHarness,
) -> ReplStartupConfig:
    """Build the secret-free effective configuration shown by the REPL."""

    provider = str(getattr(args, "provider", None) or "environment")
    base_url = str(getattr(harness.model, "base_url", "-"))
    credential_configured = False
    try:
        from evopi.ai import resolve_model_environment

        model_environment = resolve_model_environment(
            getattr(args, "provider", None),
            model=getattr(args, "model", None),
        )
        provider = model_environment.provider
        base_url = model_environment.base_url
        credential_configured = model_environment.credential_configured
    except ValueError:
        pass
    route = harness.model_route
    fallbacks = (
        tuple(
            f"{candidate.provider}:{candidate.model.name}"
            for candidate in route.candidates[1:]
        )
        if route is not None
        else ()
    )
    return ReplStartupConfig(
        provider=provider,
        model=harness.model.name,
        base_url=base_url,
        workspace=str(harness.workspace),
        session_mode=(
            "persistent" if harness.session.is_persistent else "memory"
        ),
        retry_enabled=not bool(getattr(args, "no_retry", False)),
        max_retries=int(getattr(args, "max_retries", 3)),
        deadline=getattr(args, "deadline", None),
        tool_timeout=getattr(args, "tool_timeout", None),
        fallbacks=fallbacks,
        included_tools=_split_selection(getattr(args, "tools", None)),
        excluded_tools=_split_selection(getattr(args, "exclude_tools", None)),
        credential_configured=credential_configured,
    )


def _split_selection(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(name.strip() for name in value.split(",") if name.strip())


class ReplCommandRegistry:
    """Single source of truth for built-in REPL commands and dispatch."""

    def __init__(self) -> None:
        specs = _builtin_specs()
        self._specs = {spec.name: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise ValueError("REPL command names must be unique")

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    @property
    def reserved_plugin_commands(self) -> frozenset[str]:
        return frozenset(self.command_names)

    def specs(self, context: ReplCommandContext) -> tuple[ReplCommandSpec, ...]:
        plugin_specs = tuple(
            ReplCommandSpec(
                name=command.name,
                usage=command.usage or command.name,
                description=(
                    command.description
                    or f"Plugin command from {command.runtime_plugin_name}"
                ),
                group="Plugin",
                source=f"plugin:{command.runtime_plugin_name}",
                handler=_plugin_placeholder,
            )
            for command in context.harness.plugin_commands
        )
        return tuple(sorted((*self._specs.values(), *plugin_specs), key=lambda item: item.name))

    async def dispatch(
        self,
        context: ReplCommandContext,
        text: str,
    ) -> ReplCommandResult:
        stripped = text.strip()
        command, _, arguments = stripped.partition(" ")
        name = "/" + command.lstrip("/").lower()
        spec = self._specs.get(name)
        if spec is not None:
            try:
                result = spec.handler(context, arguments.strip(), stripped)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except Exception as exc:
                context.console.print(f"[red]Error: {exc}[/]")
                return ReplCommandResult()
        try:
            handled = await context.harness.dispatch_plugin_command(stripped)
        except Exception as exc:
            context.console.print(f"[red]Plugin command error: {exc}[/]")
            return ReplCommandResult()
        if handled:
            return ReplCommandResult()
        context.console.print(
            Panel(
                f"Unknown command: [bold]{name}[/]\n\n"
                "Type [bold]/help[/] to see available commands.",
                border_style="red",
                title="Error",
            )
        )
        return ReplCommandResult()


class ReplCompleter(Completer):
    """Dynamic completion backed by the live command and Session views."""

    def __init__(
        self,
        *,
        registry: ReplCommandRegistry,
        context: ReplCommandContext,
    ) -> None:
        self._registry = registry
        self._context = context

    def get_completions(self, document: Document, complete_event):
        del complete_event
        text = document.text_before_cursor
        parts = text.split()
        if text.startswith("/help "):
            fragment = parts[-1] if len(parts) > 1 else ""
            for spec in self._registry.specs(self._context):
                candidate = spec.name.lstrip("/")
                if candidate.startswith(fragment):
                    yield Completion(candidate, start_position=-len(fragment))
            return
        if text.startswith("/switch ") or text.startswith("/merge "):
            fragment = parts[-1] if len(parts) > 1 else ""
            for leaf_id in self._context.harness.session.leaves():
                candidate = leaf_id[:16]
                if candidate.startswith(fragment):
                    yield Completion(candidate, start_position=-len(fragment))
            return
        if " " in text:
            return
        fragment = text
        for spec in self._registry.specs(self._context):
            if spec.name.startswith(fragment):
                yield Completion(
                    spec.name,
                    start_position=-len(fragment),
                    display_meta=spec.source,
                )


def startup_panel(context: ReplCommandContext) -> Panel:
    """Render the concise workbench startup state."""

    harness = context.harness
    capabilities = harness.capabilities
    resources = harness.resources
    route = (
        f"{1 + len(context.startup.fallbacks)} candidates"
        if context.startup.fallbacks
        else "single"
    )
    lines = [
        f"Model: [cyan]{context.startup.model}[/] ({route})",
        f"Session: [dim]{harness.session.session_id[:12]}...[/] "
        f"({context.startup.session_mode})",
        f"Workspace: [dim]{context.startup.workspace}[/]",
        (
            f"Active: {len(capabilities.active_tool_names)} Tools · "
            f"{len(capabilities.policy_names)} Policies · "
            f"{len(capabilities.plugin_names)} Plugins"
        ),
        (
            f"Resources: Memory {'on' if resources.memory.enabled else 'off'} · "
            f"{len(resources.skills)} Skills · "
            f"SubAgent {'on' if resources.subagent_enabled else 'off'}"
        ),
    ]
    return Panel(
        "\n".join(lines),
        title="EvoPi",
        subtitle="Type /help for commands",
        border_style="blue",
    )


def _builtin_specs() -> tuple[ReplCommandSpec, ...]:
    return (
        _spec("/help", "/help [command]", "Show grouped command help", "Runtime", _help),
        _spec("/status", "/status", "Show the current workbench summary", "Runtime", _status),
        _spec("/settings", "/settings", "Show effective non-secret settings", "Runtime", _settings),
        _spec("/tools", "/tools [active|all]", "List active or registered Tools", "Runtime", _tools),
        _spec("/trace", "/trace", "Show the current Trace destination", "Runtime", _trace),
        _spec("/retry", "/retry", "Re-run the most recent prompt", "Runtime", _retry),
        _spec("/clear", "/clear", "Clear the terminal display", "Runtime", _clear),
        _spec("/quit", "/quit", "Exit the workbench", "Runtime", _quit),
        _spec("/exit", "/exit", "Exit the workbench", "Runtime", _quit),
        _spec("/session", "/session", "Show the active Session", "Session", _session),
        _spec("/new", "/new", "Create and switch to a new Session", "Session", _new),
        _spec("/tree", "/tree", "List Session branch leaves", "Session", _leaves),
        _spec("/leaves", "/leaves", "List Session branch leaves", "Session", _leaves),
        _spec("/branch", "/branch [name]", "Branch from the active leaf", "Session", _branch),
        _spec("/switch", "/switch PREFIX", "Switch to one Session leaf", "Session", _switch),
        _spec("/fork", "/fork", "Fork the Session into a new file", "Session", _fork),
        _spec("/compact", "/compact SUMMARY", "Persist a manual compact summary", "Session", _compact),
        _spec("/merge", "/merge PREFIX [SUMMARY]", "Merge branch knowledge", "Session", _merge),
        _spec("/policies", "/policies", "List assembled Policies", "Governance", _policies),
        _spec("/plugins", "/plugins", "List active Plugins", "Plugin", _plugins),
        _spec("/reload", "/reload", "Transactionally reload Plugins and Policies", "Plugin", _reload),
        _spec("/skills", "/skills", "List loaded Skill metadata", "Resources", _skills),
        _spec("/memory", "/memory", "Show Memory status and count", "Resources", _memory),
        _spec("/agents", "/agents", "Show SubAgent availability", "Resources", _agents),
    )


def _spec(
    name: str,
    usage: str,
    description: str,
    group: str,
    handler: ReplCommandHandler,
) -> ReplCommandSpec:
    return ReplCommandSpec(
        name=name,
        usage=usage,
        description=description,
        group=group,
        handler=handler,
    )


def _plugin_placeholder(
    context: ReplCommandContext,
    arguments: str,
    raw: str,
) -> ReplCommandResult:
    del context, arguments, raw
    return ReplCommandResult()


def _help(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del raw
    specs = ReplCommandRegistry().specs(context)
    requested = "/" + arguments.lstrip("/").lower() if arguments else None
    if requested is not None:
        spec = next((item for item in specs if item.name == requested), None)
        if spec is None:
            context.console.print(f"[yellow]Unknown command: {requested}[/]")
        else:
            context.console.print(
                Panel(
                    f"[bold]{escape(spec.usage)}[/]\n{escape(spec.description)}\n"
                    f"Source: {spec.source}",
                    title="Command Help",
                    border_style="blue",
                )
            )
        return ReplCommandResult()
    groups = ("Runtime", "Session", "Governance", "Resources", "Plugin")
    table = Table(border_style="blue", title="EvoPi Commands")
    table.add_column("Group", style="bold")
    table.add_column("Command", style="cyan")
    table.add_column("Description")
    for group in groups:
        for spec in specs:
            if spec.group == group:
                table.add_row(group, spec.usage, spec.description)
    context.console.print(table)
    return ReplCommandResult()


def _status(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    harness = context.harness
    capabilities = harness.capabilities
    resources = harness.resources
    table = _property_table("Workbench Status")
    table.add_row("Model", context.startup.model)
    table.add_row(
        "Route",
        " → ".join((context.startup.model, *context.startup.fallbacks)),
    )
    table.add_row("Session", harness.session.session_id)
    table.add_row("Workspace", context.startup.workspace)
    table.add_row(
        "Active tools",
        f"{len(capabilities.active_tool_names)}/{len(capabilities.tool_names)}",
    )
    table.add_row("Policies", str(len(capabilities.policy_names)))
    table.add_row("Plugins", str(len(capabilities.plugin_names)))
    table.add_row("Memory", f"{resources.memory.entry_count} entries")
    table.add_row("Skills", str(len(resources.skills)))
    table.add_row("SubAgent", "enabled" if resources.subagent_enabled else "disabled")
    table.add_row("Warnings", str(len(capabilities.warnings)))
    context.console.print(table)
    for warning in capabilities.warnings:
        context.console.print(f"[yellow]Warning: {warning}[/]")
    return ReplCommandResult()


def _settings(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    value = context.startup
    table = _property_table("Effective Settings")
    rows = (
        ("Provider", value.provider),
        ("Model", value.model),
        ("Base URL", value.base_url),
        ("Credential configured", str(value.credential_configured)),
        ("Workspace", value.workspace),
        ("Session mode", value.session_mode),
        ("Retry", f"{value.retry_enabled} ({value.max_retries})"),
        ("Deadline", str(value.deadline or "-")),
        ("Tool timeout", str(value.tool_timeout or "-")),
        ("Fallbacks", ", ".join(value.fallbacks) or "none"),
        ("Tool allowlist", ", ".join(value.included_tools or ()) or "none"),
        ("Tool exclusions", ", ".join(value.excluded_tools or ()) or "none"),
    )
    for key, item in rows:
        table.add_row(key, item)
    context.console.print(table)
    return ReplCommandResult()


def _tools(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del raw
    mode = arguments.lower() or "active"
    if mode not in {"active", "all"}:
        context.console.print("[yellow]Usage: /tools [active|all][/]")
        return ReplCommandResult()
    table = Table(border_style="blue", title=f"Tools ({mode})")
    table.add_column("Active")
    table.add_column("Name", style="bold")
    table.add_column("Effects")
    table.add_column("Source")
    for tool in context.harness.capabilities.tools:
        if mode == "active" and not tool.active:
            continue
        source = f"{tool.source}:{tool.plugin}" if tool.plugin else tool.source
        table.add_row("yes" if tool.active else "no", tool.name, ",".join(tool.effects), source)
    context.console.print(table)
    return ReplCommandResult()


def _policies(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    table = Table(border_style="blue", title="Policy Runtime")
    for column in ("Name", "Version", "Source", "Digest", "Activation", "Replaces"):
        table.add_column(column)
    for policy in context.harness.capabilities.policies:
        table.add_row(
            policy.name,
            policy.version,
            policy.source,
            (policy.artifact_digest or policy.digest)[:12],
            (policy.activation_id or "-")[:12],
            policy.replaces or "-",
        )
    context.console.print(table)
    return ReplCommandResult()


def _plugins(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    names = context.harness.capabilities.plugin_names
    context.console.print(
        Panel("\n".join(names) if names else "No active Plugins.", title="Plugins")
    )
    return ReplCommandResult()


def _skills(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    table = Table(border_style="blue", title="Skills")
    for column in ("Name", "Version", "Risk", "Source"):
        table.add_column(column)
    for skill in context.harness.resources.skills:
        table.add_row(skill.name, skill.version, skill.risk_level, skill.source)
    context.console.print(table)
    return ReplCommandResult()


def _memory(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    memory = context.harness.resources.memory
    context.console.print(
        Panel(
            f"Enabled: {memory.enabled}\nEntries: {memory.entry_count}",
            title="Memory",
        )
    )
    return ReplCommandResult()


def _agents(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    enabled = context.harness.resources.subagent_enabled
    context.console.print(
        Panel("enabled" if enabled else "disabled", title="SubAgent")
    )
    return ReplCommandResult()


def _trace(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    path = context.harness.trace_path
    context.console.print(Panel(str(path) if path is not None else "disabled", title="Trace"))
    return ReplCommandResult()


def _session(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    session = context.harness.session
    table = _property_table("Session")
    table.add_row("ID", session.session_id)
    table.add_row("Persistent", str(session.is_persistent))
    table.add_row("Messages", str(len(session.messages)))
    table.add_row("Entries", str(len(session.entries)))
    table.add_row("Leaves", str(len(session.leaves())))
    table.add_row("Active leaf", session.leaf_id or "-")
    context.console.print(table)
    return ReplCommandResult()


def _new(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    context.harness.reset()
    context.recent_prompt = None
    if context.display is not None:
        context.display.set_status(
            f"Model: {context.harness.model.name} | "
            f"Session: {context.harness.session.session_id[:12]}..."
        )
    context.console.print(f"[green]New session: {context.harness.session.session_id}[/]")
    return ReplCommandResult()


def _leaves(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    session = context.harness.session
    active = session.leaf_id
    lines = [f"[bold]{len(session.leaves())} leaf(ves):[/]"]
    for leaf_id in session.leaves():
        branch_name, preview = _leaf_details(session, leaf_id)
        marker = " [bold green]*[/]" if leaf_id == active else ""
        label = f" [{branch_name}]" if branch_name else ""
        suffix = f" — {preview}" if preview else ""
        lines.append(f"  {leaf_id[:16]}{label}{marker}{suffix}")
    context.console.print(Panel("\n".join(lines), title="Session Tree"))
    return ReplCommandResult()


def _leaf_details(session: Any, leaf_id: str) -> tuple[str, str]:
    branch_name = ""
    preview = ""
    current: str | None = leaf_id
    while current is not None:
        entry = session.get_entry(current)
        if not preview and getattr(entry, "type", None) == "message":
            preview = entry.message.content.replace("\n", " ").strip()[:48]
        if not branch_name and getattr(entry, "type", None) == "branch":
            branch_name = entry.branch_name
        current = entry.parent_id
    return branch_name, preview


def _branch(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del raw
    session = context.harness.session
    if session.leaf_id is None:
        context.console.print("[yellow]No active leaf to branch from.[/]")
        return ReplCommandResult()
    entry = session.branch(
        from_entry_id=session.leaf_id,
        branch_name=arguments.strip(),
    )
    context.console.print(f"[green]Branch: {entry.entry_id[:16]}[/]")
    return ReplCommandResult()


def _switch(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del raw
    if not arguments:
        context.console.print("[yellow]Usage: /switch PREFIX[/]")
        return ReplCommandResult()
    context.harness.switch_session_leaf(arguments)
    context.console.print(f"[green]Switched to {arguments}[/]")
    return ReplCommandResult()


def _fork(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    forked = context.harness.session.fork()
    try:
        context.console.print(
            Panel(
                f"Session: {forked.session_id}\nPath: {forked.session_path}",
                title="Fork Created",
            )
        )
    finally:
        forked.close()
    return ReplCommandResult()


def _compact(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del raw
    if not arguments:
        context.console.print("[yellow]Usage: /compact SUMMARY[/]")
        return ReplCommandResult()
    entry = context.harness.compact_session(arguments)
    context.console.print(f"[green]Compacted: {entry.entry_id[:16]}[/]")
    return ReplCommandResult()


async def _merge(
    context: ReplCommandContext,
    arguments: str,
    raw: str,
) -> ReplCommandResult:
    del raw
    source, _, summary = arguments.partition(" ")
    if not source:
        context.console.print("[yellow]Usage: /merge PREFIX [SUMMARY][/]")
        return ReplCommandResult()
    result = await context.harness.merge_session_branch(
        source,
        summary=summary.strip() or None,
    )
    context.console.print(f"[green]Merged: {result.entry_id[:16]}[/]")
    return ReplCommandResult()


def _reload(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    capabilities = context.harness.reload_runtime()
    context.console.print(
        f"[green]Reloaded {len(capabilities.plugin_names)} Plugin(s) and "
        f"{len(capabilities.policy_names)} Policy(s).[/]"
    )
    return ReplCommandResult()


def _retry(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    if context.recent_prompt is None:
        context.console.print("[yellow]No previous prompt to retry.[/]")
        return ReplCommandResult()
    return ReplCommandResult(action="retry", prompt=context.recent_prompt)


def _clear(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del arguments, raw
    context.console.clear()
    return ReplCommandResult()


def _quit(context: ReplCommandContext, arguments: str, raw: str) -> ReplCommandResult:
    del context, arguments, raw
    return ReplCommandResult(action="quit")


def _property_table(title: str) -> Table:
    table = Table(border_style="blue", title=title)
    table.add_column("Property", style="bold")
    table.add_column("Value")
    return table


def workspace_warning_lines(warnings: Iterable[str]) -> tuple[str, ...]:
    return tuple(f"Warning: {warning}" for warning in warnings)


__all__ = [
    "ReplCommandAction",
    "ReplCommandContext",
    "ReplCommandRegistry",
    "ReplCommandResult",
    "ReplCommandSpec",
    "ReplCompleter",
    "ReplStartupConfig",
    "build_repl_startup_config",
    "startup_panel",
    "workspace_warning_lines",
]
