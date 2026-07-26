"""Base Harness: lifecycle, hooks, Policy dispatch, context and Trace."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from evopi.core.agent import Agent
from evopi.core.agent_loop import BeforeToolCallResult
from evopi.core.cancellation import (
    AbortController,
    AbortSignal,
    call_with_optional_signal,
)
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent, EventListener
from evopi.core.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from evopi.core.model import Model
from evopi.core.model_errors import ModelErrorInfo, ModelRetryConfig
from evopi.core.tool import Tool, ToolCall, ToolResult
from evopi.harness.context_manager import ContextManager, ContextProvider
from evopi.harness.capabilities import HarnessCapabilities
from evopi.harness.confirmation import (
    ConfirmationHandler,
    ConfirmationRequest,
    ConfirmationResponse,
)
from evopi.harness.lifecycle import Lifecycle
from evopi.harness.policy_manager import PolicyManager
from evopi.harness.tool_manager import ToolManager
from evopi.policy.approval import ApprovalMode, ApprovalStore
from evopi.policy.decisions import PolicyEvaluation
from evopi.policy.registry import PolicyPack
from evopi.policy.types import Policy, PolicyContext
from evopi.session import (
    RuntimeFingerprint,
    SessionError,
    SessionManager,
    build_runtime_fingerprint,
)
from evopi.session.compact import (
    DEFAULT_COMPACTION_SETTINGS,
    CompactionSettings,
    assemble_context,
    compact_session,
    estimate_context_tokens,
    should_compact,
)
from evopi.session.tree import CompactEntry
from evopi.trace.events import TraceRecord
from evopi.trace.writer import JsonlTraceWriter

_logger = logging.getLogger(__name__)
_PLUGIN_ACTIVE_TOOLS_STATE_KEY = "_evopi.active_tools"


class PolicyBlockedError(RuntimeError):
    pass


class _ObservedPluginUI:
    """Emit shape-only UI events without recording user-visible or entered text."""

    def __init__(
        self,
        *,
        plugin_name: str,
        delegate: Any,
        emit: Callable[[CoreEvent], Awaitable[None]],
    ) -> None:
        self._plugin_name = plugin_name
        self._delegate = delegate
        self._emit = emit

    async def notify(self, message: str, *, level: str = "info") -> None:
        await self._call(
            "notify",
            lambda: self._delegate.notify(message, level=level),
            request={"level": level},
        )

    async def confirm(self, title: str, message: str) -> bool:
        result = await self._call(
            "confirm",
            lambda: self._delegate.confirm(title, message),
        )
        return bool(result)

    async def select(self, title: str, options: Sequence[str]) -> str:
        result = await self._call(
            "select",
            lambda: self._delegate.select(title, options),
            request={"option_count": len(options)},
        )
        return str(result)

    async def input(self, title: str, prompt: str = "") -> str:
        result = await self._call(
            "input",
            lambda: self._delegate.input(title, prompt),
        )
        return str(result)

    async def set_status(self, key: str, text: str | None) -> None:
        await self._call(
            "set_status",
            lambda: self._delegate.set_status(key, text),
            request={"key": key, "cleared": text is None},
        )

    async def _call(
        self,
        operation: str,
        callback: Callable[[], Awaitable[Any]],
        *,
        request: dict[str, Any] | None = None,
    ) -> Any:
        await self._emit(
            CoreEvent(
                type="plugin_ui_request",
                data={
                    "plugin": self._plugin_name,
                    "operation": operation,
                    **(request or {}),
                },
            )
        )
        try:
            result = await callback()
        except Exception as exc:
            await self._emit(
                CoreEvent(
                    type="plugin_ui_response",
                    data={
                        "plugin": self._plugin_name,
                        "operation": operation,
                        "success": False,
                        "error_type": type(exc).__name__,
                    },
                )
            )
            raise
        response: dict[str, Any] = {
            "plugin": self._plugin_name,
            "operation": operation,
            "success": True,
        }
        if operation == "confirm":
            response["approved"] = bool(result)
        await self._emit(CoreEvent(type="plugin_ui_response", data=response))
        return result


class BaseHarness:
    def __init__(
        self,
        *,
        model: Model,
        system_prompt: str = "",
        trace_path: str | Path | None = None,
        max_turns: int = 20,
        retry_config: ModelRetryConfig | None = None,
        tool_timeout: float | None = None,
        deadline: float | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
        session_manager: SessionManager | None = None,
        approvals_path: str | Path | None = None,
        approval_mode: ApprovalMode = "warn",
        compaction_settings: CompactionSettings | None = None,
        plugin_paths: list[str | Path] | None = None,
        enabled_plugins: set[str] | None = None,
        reserved_plugin_commands: frozenset[str] = frozenset(),
        plugin_ui: Any | None = None,
        memory_enabled: bool = False,
        skills_enabled: bool = False,
        assembly_warnings: tuple[str, ...] = (),
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.tool_timeout = tool_timeout
        self.deadline = deadline
        self.compaction_settings = compaction_settings or DEFAULT_COMPACTION_SETTINGS
        self.tools = ToolManager()
        self.policies = PolicyManager(
            ApprovalStore(approvals_path, mode=approval_mode)
        )
        self.context = ContextManager()
        self.lifecycle = Lifecycle()
        self.session = session_manager or SessionManager.in_memory()
        self._memory_enabled = memory_enabled
        self._skills_enabled = skills_enabled
        self._assembly_warnings = assembly_warnings
        self._run_started_at: float | None = None
        self._internal_abort_controller: AbortController | None = None
        self._reserved_plugin_commands = frozenset(
            "/" + name.lstrip("/").lower()
            for name in reserved_plugin_commands
        )
        if plugin_ui is None:
            from evopi.plugins import NullPluginUI

            plugin_ui = NullPluginUI()
        self._plugin_ui = plugin_ui

        # Plugin system
        from evopi.plugins import PluginLoader, filtered_event_listener, wire_plugins

        _ws = (
            getattr(session_manager, "workspace", Path.cwd())
            if session_manager
            else Path.cwd()
        )
        self.plugin_loader = PluginLoader(
            workspace=_ws,
            extra_paths=list(plugin_paths) if plugin_paths else None,
            discover_defaults=False,
        )
        self._workspace = Path(_ws).expanduser().resolve()
        apis = wire_plugins(self.plugin_loader, enabled=enabled_plugins)
        self._pending_plugin_events: list[tuple[str, str, Any]] = []
        self._plugin_apis = {api.plugin_name: api for api in apis}
        self._plugin_tool_overrides: set[tuple[str, str]] = set()
        self._plugin_active_overrides: dict[
            str, tuple[str, frozenset[str]]
        ] = {}
        self._plugin_prompt_fragments: list[tuple[str, Any]] = []
        self._plugin_context_providers: list[ContextProvider] = []
        self._pending_plugin_runtime_events: list[CoreEvent] = []
        for api in apis:
            for tool in api.tools:
                replace = bool(tool.metadata.get("plugin_replace"))
                self.tools.register(tool, replace=replace)
                if replace:
                    self._plugin_tool_overrides.add((api.plugin_name, tool.name))
            for policy in api.policies:
                self.policies.register(policy)
            for pack in api.policy_packs:
                for policy in pack.policies:
                    self.policies.register(policy)
            for provider in api.context_providers:
                self.context.add(provider)
                self._plugin_context_providers.append(provider)
            self._plugin_prompt_fragments.extend(
                (api.plugin_name, fragment)
                for fragment in api.prompt_fragments
            )
            self._pending_plugin_events.extend(
                (api.plugin_name, event_type, handler)
                for event_type, handler in api.events
            )
        # Collect commands from all plugins for CLI dispatch
        self._plugin_commands: dict[str, Any] = {}
        for api in apis:
            for command in api.commands:
                if command.name in self._reserved_plugin_commands:
                    raise ValueError(
                        f"Plugin command '{command.name}' is reserved by the host"
                    )
                if command.name in self._plugin_commands:
                    raise ValueError(
                        f"Plugin command '{command.name}' is registered more than once"
                    )
                self._plugin_commands[command.name] = command
        self._plugin_names = tuple(sorted(api.plugin_name for api in apis))
        self.trace_path = Path(trace_path).resolve() if trace_path is not None else None
        self.trace_writer = JsonlTraceWriter(trace_path) if trace_path is not None else None
        self.confirmation_handler = confirmation_handler
        self._runtime_fingerprint: RuntimeFingerprint | None = None
        self._session_started_emitted = False
        self._session_failure: SessionError | None = None
        self._pending_session_events: list[CoreEvent] = []
        self.agent = Agent(
            model=model,
            system_prompt=system_prompt,
            max_turns=max_turns,
            retry_config=retry_config or ModelRetryConfig(enabled=True),
            deadline=deadline,
            tool_timeout=tool_timeout,
            before_tool_call=self._before_tool_call,
            after_tool_call=self._after_tool_call,
            prepare_context=self._prepare_context,
            after_model_call=self._after_model_call,
            should_stop_after_turn=self._should_stop_after_turn,
        )
        self.agent.messages.extend(self.session.messages)
        self.agent.subscribe(self._on_core_event)
        for api in apis:
            self._bind_plugin_api(api)
        # Subscribe plugin event handlers
        self._plugin_event_unsubscribers = [
            self.agent.subscribe(
                filtered_event_listener(
                    event_type,
                    handler,
                    plugin_name=plugin_name,
                    on_contract_error=self._record_plugin_handler_error,
                )
            )
            for plugin_name, event_type, handler in self._pending_plugin_events
        ]

    @property
    def state(self):
        return self.lifecycle.state

    @property
    def capabilities(self) -> HarnessCapabilities:
        """Return a read-only snapshot of the currently assembled capabilities."""

        return HarnessCapabilities(
            tool_names=tuple(sorted(tool.name for tool in self.tools.all())),
            policy_names=tuple(sorted(policy.name for policy in self.policies.all())),
            plugin_names=self._plugin_names,
            command_names=tuple(sorted(self._plugin_commands)),
            memory_enabled=self._memory_enabled,
            skills_enabled=self._skills_enabled,
            warnings=tuple(self.plugin_loader.errors) + self._assembly_warnings,
        )

    @property
    def messages(self):
        return self.agent.messages

    @property
    def signal(self) -> AbortSignal | None:
        if self.agent.signal is not None:
            return self.agent.signal
        if self._internal_abort_controller is not None:
            return self._internal_abort_controller.signal
        return None

    @property
    def is_running(self) -> bool:
        return self.agent.is_running

    def plugin_can_override_tool(self, plugin_name: str, tool_name: str) -> bool:
        return (plugin_name, tool_name) in self._plugin_tool_overrides

    @property
    def plugin_commands(self):
        """Return immutable descriptions of commands registered by Plugins."""

        return tuple(
            self._plugin_commands[name]
            for name in sorted(self._plugin_commands)
        )

    async def dispatch_plugin_command(self, text: str) -> bool:
        """Dispatch one Plugin command through the public Harness boundary."""

        from evopi.plugins import PluginCommandContext

        stripped = text.strip()
        if not stripped:
            return False
        command_name, _, arguments = stripped.partition(" ")
        command_name = "/" + command_name.lstrip("/").lower()
        command = self._plugin_commands.get(command_name)
        if command is None:
            return False
        api = self._plugin_apis[command.runtime_plugin_name]
        assert api.runtime is not None
        context = PluginCommandContext(
            command_name=command_name,
            raw=stripped,
            runtime=api.runtime,
        )
        await self.agent.emit_event(
            CoreEvent(
                type="plugin_command_start",
                data={"plugin": api.plugin_name, "command": command_name},
            )
        )
        try:
            result = self._call_plugin_command(
                command.handler,
                arguments.strip(),
                context,
            )
            if inspect.isawaitable(result):
                await result
            await self._emit_pending_plugin_runtime_events()
        except Exception as exc:
            await self.agent.emit_event(
                CoreEvent(
                    type="plugin_command_error",
                    data={
                        "plugin": api.plugin_name,
                        "command": command_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            )
            raise
        await self.agent.emit_event(
            CoreEvent(
                type="plugin_command_end",
                data={"plugin": api.plugin_name, "command": command_name},
            )
        )
        return True

    @staticmethod
    def _call_plugin_command(handler, arguments: str, context):
        parameters = list(inspect.signature(handler).parameters.values())
        if len(parameters) >= 2:
            return handler(arguments, context)
        return handler(context.raw)

    @property
    def remaining_deadline(self) -> float | None:
        if self.deadline is None:
            return None
        if self._run_started_at is None:
            return self.deadline
        return max(0.0, self.deadline - (time.monotonic() - self._run_started_at))

    def abort(self) -> None:
        self.lifecycle.request_abort()
        self.agent.abort()
        if self._internal_abort_controller is not None:
            self._internal_abort_controller.abort()

    async def wait_for_idle(self) -> None:
        await self.agent.wait_for_idle()

    def switch_session_leaf(self, leaf_id: str):
        """Persist tree navigation and atomically replace the Agent transcript."""

        if self.is_running:
            raise RuntimeError("Cannot switch Session leaf while the Harness is running")
        selected = self.session.switch_leaf(leaf_id)
        self._restore_plugin_overrides()
        restored: list[Message] = []
        if self.system_prompt:
            restored.append(SystemMessage(content=self.system_prompt))
        restored.extend(self.session.messages)
        self.agent.messages[:] = restored
        if self.trace_writer is not None:
            self.trace_writer.write(
                TraceRecord(
                    type="session_leaf_selected",
                    data={
                        "session_id": self.session.session_id,
                        "entry_id": selected.entry_id,
                        "target_entry_id": selected.parent_id,
                        "from_entry_id": selected.from_entry_id,
                    },
                )
            )
        return selected

    def _bind_plugin_api(self, api) -> None:
        from evopi.plugins import PluginRuntimeContext

        api.state.bind(
            get_values=lambda: self.session.plugin_state(api.plugin_name),
            set_value=lambda key, value: self._set_plugin_state(
                api.plugin_name,
                api.plugin_version,
                key,
                value,
            ),
            delete_value=lambda key: self._delete_plugin_state(
                api.plugin_name,
                api.plugin_version,
                key,
            ),
        )
        api.tools.bind(
            get_all=self.tools.all,
            get_active=self._active_tools,
            set_active=self._set_plugin_active_tools,
            clear_active=self._clear_plugin_active_tools,
        )
        observed_ui = _ObservedPluginUI(
            plugin_name=api.plugin_name,
            delegate=self._plugin_ui,
            emit=self.agent.emit_event,
        )
        runtime = PluginRuntimeContext(
            plugin_name=api.plugin_name,
            plugin_version=api.plugin_version,
            workspace=str(self._workspace),
            session_id=self.session.session_id,
            tools=api.tools,
            state=api.state,
            ui=observed_ui,
        )
        api.bind_runtime(runtime, ui=observed_ui)
        stored_version = self.session.plugin_state_version(api.plugin_name)
        if stored_version is not None and stored_version != api.plugin_version:
            warning = (
                f"Plugin '{api.plugin_name}' state was written by version "
                f"{stored_version}; current version is {api.plugin_version}"
            )
            if warning not in self._assembly_warnings:
                self._assembly_warnings += (warning,)
        self._restore_plugin_override(api.plugin_name)

    def attach_plugin_ui(self, ui) -> None:
        """Attach a host-neutral UI while the Harness is idle."""

        if self.is_running:
            raise RuntimeError("Cannot replace Plugin UI while the Harness is running")
        self._plugin_ui = ui
        for api in self._plugin_apis.values():
            self._bind_plugin_api(api)

    def _active_tools(self) -> list[Tool]:
        tools = self.tools.all()
        if not self._plugin_active_overrides:
            return tools
        allowed = {tool.name for tool in tools}
        for _, names in self._plugin_active_overrides.values():
            allowed.intersection_update(names)
        return [tool for tool in tools if tool.name in allowed]

    def _set_plugin_active_tools(
        self,
        plugin_name: str,
        names: tuple[str, ...],
        scope: str,
    ) -> None:
        from evopi.plugins import PluginContractError

        if scope not in {"run", "session"}:
            raise PluginContractError("Plugin Tool scope must be 'run' or 'session'")
        known = {tool.name for tool in self.tools.all()}
        unknown = sorted(set(names) - known)
        if unknown:
            raise PluginContractError(
                "Unknown active Tool name(s): " + ", ".join(unknown)
            )
        self._plugin_active_overrides[plugin_name] = (
            scope,
            frozenset(names),
        )
        if scope == "session":
            api = self._plugin_apis[plugin_name]
            self._set_plugin_state(
                plugin_name,
                api.plugin_version,
                _PLUGIN_ACTIVE_TOOLS_STATE_KEY,
                list(names),
            )
        self.agent.tools = self._active_tools()
        self._pending_plugin_runtime_events.append(
            CoreEvent(
                type="plugin_tools_changed",
                data={
                    "plugin": plugin_name,
                    "scope": scope,
                    "active_tools": sorted(names),
                },
            )
        )

    def _clear_plugin_active_tools(self, plugin_name: str) -> None:
        self._plugin_active_overrides.pop(plugin_name, None)
        if _PLUGIN_ACTIVE_TOOLS_STATE_KEY in self.session.plugin_state(plugin_name):
            api = self._plugin_apis[plugin_name]
            self._delete_plugin_state(
                plugin_name,
                api.plugin_version,
                _PLUGIN_ACTIVE_TOOLS_STATE_KEY,
            )
        self.agent.tools = self._active_tools()
        self._pending_plugin_runtime_events.append(
            CoreEvent(
                type="plugin_tools_changed",
                data={"plugin": plugin_name, "scope": "clear"},
            )
        )

    def _clear_run_plugin_overrides(self) -> None:
        expired = [
            plugin_name
            for plugin_name, (scope, _) in self._plugin_active_overrides.items()
            if scope == "run"
        ]
        for plugin_name in expired:
            self._plugin_active_overrides.pop(plugin_name, None)
        self.agent.tools = self._active_tools()

    def _restore_plugin_overrides(self) -> None:
        self._plugin_active_overrides.clear()
        for plugin_name in self._plugin_apis:
            self._restore_plugin_override(plugin_name)
        self.agent.tools = self._active_tools()

    def _restore_plugin_override(self, plugin_name: str) -> None:
        raw_names = self.session.plugin_state(plugin_name).get(
            _PLUGIN_ACTIVE_TOOLS_STATE_KEY
        )
        if isinstance(raw_names, list) and all(
            isinstance(name, str) for name in raw_names
        ):
            self._plugin_active_overrides[plugin_name] = (
                "session",
                frozenset(raw_names),
            )

    def _set_plugin_state(
        self,
        plugin_name: str,
        plugin_version: str,
        key: str,
        value: Any,
    ) -> None:
        entry = self.session.append_plugin_state(
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            key=key,
            value=value,
        )
        self._record_plugin_state_event(entry)

    def _delete_plugin_state(
        self,
        plugin_name: str,
        plugin_version: str,
        key: str,
    ) -> None:
        entry = self.session.append_plugin_state(
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            key=key,
            operation="delete",
        )
        self._record_plugin_state_event(entry)

    def _record_plugin_state_event(self, entry) -> None:
        value_digest = hashlib.sha256(
            json.dumps(
                entry.value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._pending_plugin_runtime_events.append(
            CoreEvent(
                type="plugin_state_changed",
                data={
                    "plugin": entry.plugin_name,
                    "plugin_version": entry.plugin_version,
                    "key": entry.key,
                    "operation": entry.operation,
                    "value_sha256": value_digest,
                    "entry_id": entry.entry_id,
                },
            )
        )

    def _record_plugin_handler_error(self, message: str) -> None:
        _logger.warning("%s", message)
        if self.trace_writer is not None:
            self.trace_writer.write(
                TraceRecord(
                    type="plugin_handler_error",
                    data={"error": message},
                )
            )

    async def _emit_pending_plugin_runtime_events(self) -> None:
        events = self._pending_plugin_runtime_events
        self._pending_plugin_runtime_events = []
        for event in events:
            await self.agent.emit_event(event)

    def register_tool(self, tool: Tool, *, replace: bool = False) -> None:
        if "effects" not in tool.metadata:
            tool.metadata["effects"] = ["unknown"]
        self.tools.register(tool, replace=replace)
        self.agent.tools = self._active_tools()

    def register_policy(self, policy: Policy, *, replace: bool = False) -> None:
        self.policies.register(policy, replace=replace)

    def load_policy_pack(self, pack: PolicyPack) -> None:
        self.policies.load_pack(pack)

    def add_context_provider(self, provider: ContextProvider) -> None:
        self.context.add(provider)

    def subscribe(self, listener: EventListener):
        return self.agent.subscribe(listener)

    async def prompt(self, content: str) -> AssistantMessage:
        self.agent.tools = self._active_tools()
        await self._emit_pending_plugin_runtime_events()
        self._runtime_fingerprint = self._build_runtime_fingerprint()
        self.session.compare_runtime(self._runtime_fingerprint)
        self.lifecycle.start()
        self._run_started_at = time.monotonic()
        try:
            answer = await self.agent.prompt(content)
        except asyncio.CancelledError:
            await self._emit_pending_session_events()
            error = self.agent.last_run.error if self.agent.last_run is not None else None
            self.lifecycle.abort(error)
            self._run_started_at = None
            self._clear_run_plugin_overrides()
            raise
        except Exception as exc:
            await self._emit_pending_session_events()
            end_reason = (
                self.agent.last_run.end_reason
                if self.agent.last_run is not None
                else "error"
            )
            self.lifecycle.fail(exc, end_reason=end_reason)
            self._run_started_at = None
            self._clear_run_plugin_overrides()
            raise
        await self._emit_pending_session_events()
        end_reason = (
            self.agent.last_run.end_reason
            if self.agent.last_run is not None
            else "completed"
        )
        if end_reason == "aborted":
            error = self.agent.last_run.error if self.agent.last_run is not None else None
            self.lifecycle.abort(error)
        else:
            self.lifecycle.complete(end_reason=end_reason)

        # Auto-compaction check (best-effort, never crash)
        if end_reason not in ("aborted", "error", "turn_limit"):
            try:
                await self._maybe_compact()
            except Exception:
                _logger.warning("Compaction check failed", exc_info=True)
        self._run_started_at = None
        self._clear_run_plugin_overrides()
        return answer

    def reset(self) -> None:
        if self.is_running:
            raise RuntimeError("Cannot reset a running Harness")
        replacement = self.session.new_session()
        self.agent.reset()
        self.session = replacement
        self.lifecycle.reset()
        self._runtime_fingerprint = None
        self._session_started_emitted = False
        self._session_failure = None
        self._pending_session_events.clear()
        self._pending_plugin_runtime_events.clear()
        self._plugin_active_overrides.clear()
        for api in self._plugin_apis.values():
            self._bind_plugin_api(api)

    def close(self) -> None:
        if self.is_running:
            raise RuntimeError("Cannot close a running Harness")
        self.session.close()

    def reload_plugins(self) -> HarnessCapabilities:
        """Transactionally replace Plugin contributions from approved snapshots."""

        if self.is_running:
            raise RuntimeError("Cannot reload Plugins while the Harness is running")
        from evopi.plugins import (
            PluginLoader,
            approved_plugin_entrypoints,
            filtered_event_listener,
            wire_plugins,
        )

        paths: list[str | Path] = list(
            approved_plugin_entrypoints(self._workspace)
        )
        loader = PluginLoader(
            workspace=self._workspace,
            extra_paths=paths,
            discover_defaults=False,
        )
        apis = wire_plugins(loader)
        if loader.errors:
            raise RuntimeError(
                "Plugin reload validation failed: " + "; ".join(loader.errors)
            )

        old_plugin_names = set(self._plugin_names)
        next_tools = ToolManager()
        for tool in self.tools.all():
            if tool.metadata.get("plugin_source") not in old_plugin_names:
                next_tools.register(tool)
        next_policies = PolicyManager(self.policies.approval_store)
        for policy in self.policies.all():
            if policy.metadata.get("plugin_source") not in old_plugin_names:
                next_policies.register(policy)

        commands: dict[str, Any] = {}
        pending_events: list[tuple[str, str, Any]] = []
        prompt_fragments: list[tuple[str, Any]] = []
        context_providers: list[ContextProvider] = []
        tool_overrides: set[tuple[str, str]] = set()
        for api in apis:
            for tool in api.tools:
                replace = bool(tool.metadata.get("plugin_replace"))
                next_tools.register(
                    tool,
                    replace=replace,
                )
                if replace:
                    tool_overrides.add((api.plugin_name, tool.name))
            for policy in api.policies:
                next_policies.register(policy)
            for pack in api.policy_packs:
                for policy in pack.policies:
                    next_policies.register(policy)
            for command in api.commands:
                if command.name in self._reserved_plugin_commands:
                    raise RuntimeError(
                        f"Plugin command '{command.name}' is reserved by the host"
                    )
                if command.name in commands:
                    raise RuntimeError(
                        f"Plugin command '{command.name}' is registered more than once"
                    )
                commands[command.name] = command
            pending_events.extend(
                (api.plugin_name, event_type, handler)
                for event_type, handler in api.events
            )
            context_providers.extend(api.context_providers)
            prompt_fragments.extend(
                (api.plugin_name, fragment)
                for fragment in api.prompt_fragments
            )

        for unsubscribe in self._plugin_event_unsubscribers:
            unsubscribe()
        for provider in self._plugin_context_providers:
            self.context.remove(provider)
        self.tools = next_tools
        self.policies = next_policies
        self.plugin_loader = loader
        self._plugin_commands = commands
        self._plugin_names = tuple(sorted(api.plugin_name for api in apis))
        self._plugin_apis = {api.plugin_name: api for api in apis}
        self._plugin_tool_overrides = tool_overrides
        self._plugin_prompt_fragments = prompt_fragments
        self._plugin_context_providers = context_providers
        self._plugin_active_overrides = {
            name: value
            for name, value in self._plugin_active_overrides.items()
            if name in self._plugin_apis
        }
        for provider in context_providers:
            self.context.add(provider)
        for api in apis:
            self._bind_plugin_api(api)
        self._pending_plugin_events = pending_events
        self._plugin_event_unsubscribers = [
            self.agent.subscribe(
                filtered_event_listener(
                    event_type,
                    handler,
                    plugin_name=plugin_name,
                    on_contract_error=self._record_plugin_handler_error,
                )
            )
            for plugin_name, event_type, handler in pending_events
        ]
        self.agent.tools = self._active_tools()
        self._refresh_system_prompt_after_capability_change()
        if self.trace_writer is not None:
            self.trace_writer.write(
                TraceRecord(
                    type="plugin_reload",
                    data={
                        "plugins": list(self._plugin_names),
                        "tools": [
                            tool.name for tool in self.tools.all()
                        ],
                    },
                )
            )
        return self.capabilities

    def _refresh_system_prompt_after_capability_change(self) -> None:
        """Domain Harness hook for capability-derived prompts."""

    async def _prepare_context(
        self,
        context: AgentContext,
        *,
        signal: AbortSignal | None = None,
    ) -> AgentContext:
        prepared = await self.context.prepare(context, signal=signal)
        prepared.tools = self._active_tools()
        if self._plugin_prompt_fragments:
            from evopi.plugins import PluginPromptContext

            active_names = tuple(tool.name for tool in prepared.tools)
            fragments = sorted(
                self._plugin_prompt_fragments,
                key=lambda item: (item[1].priority, item[0], item[1].name),
            )
            for plugin_name, fragment in fragments:
                api = self._plugin_apis[plugin_name]
                prompt_context = PluginPromptContext(
                    plugin_name=plugin_name,
                    plugin_version=api.plugin_version,
                    workspace=str(self._workspace),
                    session_id=self.session.session_id,
                    active_tools=active_names,
                    state=api.state.snapshot(),
                )
                value = fragment.provider(prompt_context)
                if inspect.isawaitable(value):
                    value = await value
                if value:
                    prepared.messages.insert(0, SystemMessage(content=value))
                    await self.agent.emit_event(
                        CoreEvent(
                            type="plugin_prompt_applied",
                            data={
                                "plugin": plugin_name,
                                "fragment": fragment.name,
                            },
                        )
                    )
        evaluation = await self._evaluate(
            PolicyContext(
                hook="before_model_call",
                agent_context=prepared,
                aborted=bool(signal and signal.aborted),
            )
        )
        if not (signal and signal.aborted):
            self._raise_if_blocked(evaluation, "Model call")
        return prepared

    async def _after_model_call(
        self,
        context: AgentContext,
        assistant: AssistantMessage,
        *,
        signal: AbortSignal | None = None,
    ) -> AssistantMessage:
        evaluation = await self._evaluate(
            PolicyContext(
                hook="after_model_call",
                agent_context=context,
                assistant_message=assistant,
                aborted=bool(signal and signal.aborted),
            )
        )
        if not (signal and signal.aborted):
            self._raise_if_blocked(evaluation, "Model response")
        return assistant

    async def _before_tool_call(
        self,
        context: AgentContext,
        assistant: AssistantMessage,
        call: ToolCall,
        *,
        signal: AbortSignal | None = None,
    ) -> BeforeToolCallResult:
        tool = self.tools.registry.get(call.name)
        evaluation = await self._evaluate(
            PolicyContext(
                hook="before_tool_call",
                agent_context=context,
                assistant_message=assistant,
                tool_call=call,
                arguments=dict(call.arguments),
                aborted=bool(signal and signal.aborted),
                tool_plugin_source=tool.metadata.get("plugin_source") if tool else None,
            )
        )
        final = evaluation.final
        blocked = final.action == "block"
        reason = final.reason
        if final.action == "require_confirmation":
            request = ConfirmationRequest(
                hook="before_tool_call",
                reason=reason or "Human confirmation is required",
                risk_level=final.risk_level,
                policy_names=tuple(
                    decision.policy_name
                    for decision in evaluation.decisions
                    if decision.action == "require_confirmation"
                    and decision.policy_name is not None
                ),
                tool_call=call,
                arguments=dict(
                    evaluation.arguments
                    if evaluation.arguments is not None
                    else call.arguments
                ),
                metadata={"session_id": self.session.session_id},
            )
            response = await self._request_confirmation(request, signal=signal)
            blocked = not response.approved
            reason = response.reason or (
                "Human confirmation approved"
                if response.approved
                else "Human confirmation denied"
            )
        return BeforeToolCallResult(
            block=blocked,
            reason=reason,
            arguments=evaluation.arguments,
        )

    async def _request_confirmation(
        self,
        request: ConfirmationRequest,
        *,
        signal: AbortSignal | None,
    ) -> ConfirmationResponse:
        self.lifecycle.wait_for_confirmation()
        try:
            await self.agent.emit_event(
                CoreEvent(type="confirmation_request", data={"request": request})
            )
            response = await self._resolve_confirmation(request, signal=signal)
            if response.decision == "cancelled" and not (signal and signal.aborted):
                self.abort()
                signal = self.agent.signal
            if signal is not None and signal.aborted:
                self.lifecycle.request_abort()
                await signal._wait_until_notified()
                response = ConfirmationResponse(
                    request_id=request.id,
                    decision="cancelled",
                    reason=response.reason or "Run aborted while waiting for confirmation",
                    metadata={**response.metadata, "automatic": True, "aborted": True},
                )
        finally:
            self.lifecycle.resume()

        await self.agent.emit_event(
            CoreEvent(type="confirmation_response", data={"response": response})
        )
        return response

    async def _resolve_confirmation(
        self,
        request: ConfirmationRequest,
        *,
        signal: AbortSignal | None,
    ) -> ConfirmationResponse:
        if self.confirmation_handler is None:
            return ConfirmationResponse(
                request_id=request.id,
                decision="deny",
                reason="Human confirmation is required but no handler is configured",
                metadata={"automatic": True},
            )

        try:
            response = call_with_optional_signal(
                self.confirmation_handler,
                request,
                signal=signal,
            )
            if inspect.isawaitable(response):
                handler_task = asyncio.ensure_future(response)
                if signal is None:
                    response = await handler_task
                else:
                    abort_task = asyncio.create_task(signal.wait())
                    done, _ = await asyncio.wait(
                        {handler_task, abort_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if abort_task in done:
                        if not handler_task.done():
                            handler_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await handler_task
                        return ConfirmationResponse(
                            request_id=request.id,
                            decision="cancelled",
                            reason="Run aborted while waiting for confirmation",
                            metadata={"automatic": True, "aborted": True},
                        )
                    abort_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await abort_task
                    response = await handler_task
        except Exception as exc:
            if signal is not None and signal.aborted:
                return ConfirmationResponse(
                    request_id=request.id,
                    decision="cancelled",
                    reason=f"Confirmation handler stopped during abort: {type(exc).__name__}: {exc}",
                    metadata={
                        "automatic": True,
                        "aborted": True,
                        "handler_error": type(exc).__name__,
                    },
                )
            return ConfirmationResponse(
                request_id=request.id,
                decision="deny",
                reason=f"Confirmation handler failed: {type(exc).__name__}: {exc}",
                metadata={"automatic": True, "handler_error": type(exc).__name__},
            )

        if not isinstance(response, ConfirmationResponse):
            return ConfirmationResponse(
                request_id=request.id,
                decision="deny",
                reason="Confirmation handler returned an invalid response",
                metadata={"automatic": True, "handler_error": "invalid_response"},
            )
        if response.request_id != request.id:
            return ConfirmationResponse(
                request_id=request.id,
                decision="deny",
                reason="Confirmation response did not match the active request",
                metadata={"automatic": True, "handler_error": "request_id_mismatch"},
            )
        return response

    async def _after_tool_call(
        self,
        context: AgentContext,
        assistant: AssistantMessage,
        call: ToolCall,
        result: ToolResult,
        *,
        signal: AbortSignal | None = None,
    ) -> ToolResult:
        evaluation = await self._evaluate(
            PolicyContext(
                hook="after_tool_call",
                agent_context=context,
                assistant_message=assistant,
                tool_call=call,
                tool_result=result,
                arguments=dict(call.arguments),
                aborted=bool(signal and signal.aborted),
            )
        )
        final_result = evaluation.tool_result or result
        if evaluation.final.action == "block":
            return ToolResult(
                content=evaluation.final.reason or "Tool result was blocked",
                is_error=True,
                metadata={**final_result.metadata, "blocked_after_execution": True},
            )
        if evaluation.final.action == "terminate":
            final_result.terminate = True
        return final_result

    async def _should_stop_after_turn(
        self,
        context: AgentContext,
        assistant: AssistantMessage,
        results: list[ToolResultMessage],
        *,
        signal: AbortSignal | None = None,
    ) -> bool:
        edited = [message.metadata.get("path") for message in results if message.tool_name == "write_file"]
        evaluation = await self._evaluate(
            PolicyContext(
                hook="after_turn",
                agent_context=context,
                assistant_message=assistant,
                aborted=bool(signal and signal.aborted),
                metadata={"edited_files": [path for path in edited if path]},
            )
        )
        return evaluation.final.action == "terminate"

    async def _evaluate(self, context: PolicyContext) -> PolicyEvaluation:
        evaluation = await self.policies.engine.evaluate(context)
        for decision in evaluation.decisions:
            event = CoreEvent(
                type="policy_decision",
                data={"hook": context.hook, "decision": decision},
            )
            await self.agent.emit_event(event)
        await self.agent.emit_event(
            CoreEvent(
                type="policy_evaluation",
                data=self._policy_evaluation_data(context, evaluation),
            )
        )
        return evaluation

    async def _on_core_event(self, event: CoreEvent) -> None:
        if event.type == "agent_start" and not self._session_started_emitted:
            self._session_started_emitted = True
            await self.agent.emit_event(
                CoreEvent(
                    type="session_start",
                    run_id=event.run_id,
                    data={
                        "session_id": self.session.session_id,
                        "reason": self.session.recovery_info.reason,
                        "persistent": self.session.is_persistent,
                        "workspace": self.session.workspace,
                        "attached_workspace": self.session.attached_workspace,
                        "checkpoint_id": self.session.recovery_info.checkpoint_id,
                        "warnings": list(self.session.recovery_info.warnings),
                    },
                )
            )
        if event.type == "agent_end" and self._session_failure is None:
            await self._persist_session_event_checked(event)
            await self._emit_pending_session_events()
            if self.trace_writer is not None:
                self.trace_writer(event)
        else:
            if self.trace_writer is not None:
                self.trace_writer(event)
            if self._session_failure is None:
                await self._persist_session_event_checked(event)
        if event.type == "abort_requested":
            self.lifecycle.request_abort()
        if event.type == "agent_end" and event.data.get("reason") == "aborted":
            self.lifecycle.abort(event.data.get("error"))
        if event.type == "error":
            context = PolicyContext(
                hook="on_error",
                agent_context=AgentContext(
                    messages=self.agent.messages,
                    tools=self.agent.tools,
                ),
                error=str(event.data.get("error", "")),
                error_info=(
                    event.data.get("error_info")
                    if isinstance(event.data.get("error_info"), ModelErrorInfo)
                    else None
                ),
                aborted=bool(self.agent.signal and self.agent.signal.aborted),
            )
            evaluation = await self.policies.engine.evaluate(context)
            if self.trace_writer is not None:
                for decision in evaluation.decisions:
                    self.trace_writer.write(
                        TraceRecord(
                            type="policy_decision",
                            run_id=event.run_id,
                            data={"hook": "on_error", "decision": decision},
                        )
                    )
                self.trace_writer.write(
                    TraceRecord(
                        type="policy_evaluation",
                        run_id=event.run_id,
                        data=self._policy_evaluation_data(context, evaluation),
                    )
                )

    async def _persist_session_event_checked(self, event: CoreEvent) -> None:
        try:
            self._persist_session_event(event)
        except SessionError as exc:
            self._session_failure = exc
            await self.agent.emit_event(
                CoreEvent(
                    type="session_error",
                    run_id=event.run_id,
                    data={
                        "operation": event.type,
                        "error": f"{type(exc).__name__}: {exc}",
                        "recoverable": False,
                    },
                )
            )
            raise

    def _persist_session_event(self, event: CoreEvent) -> None:
        if event.type == "agent_start":
            if event.run_id is None or self._runtime_fingerprint is None:
                raise RuntimeError(
                    "Session run_start requires a Run ID and runtime fingerprint"
                )
            self.session.append_run_start(
                run_id=event.run_id,
                runtime_fingerprint=self._runtime_fingerprint,
                trace_path=self.trace_path,
            )
            return
        if event.type == "message_end":
            if event.data.get("committed") is False:
                return
            message = event.data.get("message")
            if not isinstance(
                message,
                (UserMessage, AssistantMessage, ToolResultMessage),
            ):
                return
            if event.run_id is None:
                raise RuntimeError("Session message requires a Run ID")
            self.session.append_message(run_id=event.run_id, message=message)
            return
        if event.type != "agent_end" or self._runtime_fingerprint is None:
            return
        if event.run_id is None:
            raise RuntimeError("Session run_end requires a Run ID")
        reason = event.data.get("reason")
        if reason not in {
            "completed",
            "terminated",
            "aborted",
            "error",
            "turn_limit",
            "deadline_exceeded",
        }:
            raise RuntimeError(f"Unsupported Agent end reason: {reason!r}")
        error_info = event.data.get("error_info")
        run_end = self.session.append_run_end(
            run_id=event.run_id,
            reason=reason,
            error=(
                str(event.data["error"])
                if event.data.get("error") is not None
                else None
            ),
            error_info=(
                error_info if isinstance(error_info, ModelErrorInfo) else None
            ),
        )
        checkpoint = self.session.create_checkpoint(
            run_end=run_end,
            runtime_fingerprint=self._runtime_fingerprint,
        )
        if checkpoint is None:
            self._pending_session_events.append(
                CoreEvent(
                    type="session_error",
                    run_id=event.run_id,
                    data={
                        "operation": "checkpoint",
                        "error": self.session.recovery_info.warnings[-1],
                        "recoverable": True,
                    },
                )
            )
        else:
            self._pending_session_events.append(
                CoreEvent(
                    type="session_checkpoint",
                    run_id=event.run_id,
                    data={
                        "session_id": self.session.session_id,
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "active_entry_id": checkpoint.active_entry_id,
                    },
                )
            )

    async def _emit_pending_session_events(self) -> None:
        events = self._pending_session_events
        self._pending_session_events = []
        for event in events:
            await self.agent.emit_event(event)

    def _build_runtime_fingerprint(self) -> RuntimeFingerprint:
        policies = [
            {
                "name": policy.name,
                "version": policy.version,
                "hooks": list(policy.hooks),
                "priority": policy.priority,
                "enabled": policy.enabled,
                "source": policy.source,
                "risk_level": policy.risk_level,
            }
            for policy in self.policies.all()
        ]
        return build_runtime_fingerprint(
            harness=f"{type(self).__module__}.{type(self).__qualname__}",
            model=self.model.name,
            system_prompt=self.system_prompt,
            tools=[tool.definition() for tool in self.tools.all()],
            policies=policies,
        )

    async def _maybe_compact(self) -> None:
        """Check whether the context exceeds the compaction threshold and,
        if so, generate a summary and insert a ``CompactEntry`` into the
        session tree.
        """
        settings = self.compaction_settings
        if not settings.enabled:
            return
        context_window = getattr(self.model, "context_window", 0) or 0
        if context_window <= 0:
            return  # model doesn't report its window — skip

        messages: list[Message] = list(self.session.messages)  # type: ignore[assignment]
        if len(messages) < 4:
            return  # nothing worth compacting

        tools_defs = [t.definition() for t in self.tools.all()]
        context_tokens = estimate_context_tokens(
            messages, system_prompt=self.system_prompt, tools=tools_defs
        )
        if not should_compact(context_tokens, context_window, settings):
            return

        evaluation = await self._evaluate(
            PolicyContext(
                hook="before_session_compact",
                agent_context=AgentContext(
                    messages=list(self.agent.messages),
                    tools=self.tools.all(),
                ),
                arguments={
                    "context_tokens": context_tokens,
                    "context_window": context_window,
                    "reserve_tokens": settings.reserve_tokens,
                    "keep_recent_tokens": settings.keep_recent_tokens,
                },
            )
        )
        if evaluation.final.action == "block":
            _logger.warning(
                "Compaction skipped by Policy: %s",
                evaluation.final.reason or evaluation.final.action,
            )
            return
        if evaluation.final.action == "require_confirmation":
            request = ConfirmationRequest(
                hook="before_session_compact",
                reason=(
                    evaluation.final.reason
                    or "Session compaction requires confirmation"
                ),
                risk_level=evaluation.final.risk_level,
                arguments=evaluation.arguments,
                metadata={"session_id": self.session.session_id},
            )
            response = await self._request_confirmation(
                request,
                signal=self.signal,
            )
            if not response.approved:
                _logger.warning(
                    "Compaction confirmation denied: %s",
                    response.reason,
                )
                return

        _logger.info(
            "Compaction triggered: %d tokens > %d window - %d reserve",
            context_tokens,
            context_window,
            settings.reserve_tokens,
        )

        # Find previous compaction summary for incremental updates
        previous_summary: str | None = None
        for entry in reversed(list(self.session.get_active_path())):
            if isinstance(entry, CompactEntry):
                previous_summary = entry.summary
                break

        from evopi.harness.model_operation import GovernedModelOperation

        operation = GovernedModelOperation(
            parent=self,
            model=self.model,
            kind="session_compaction",
            signal_controller=AbortController(
                loop=asyncio.get_running_loop()
            ),
        )
        self._internal_abort_controller = operation.signal_controller
        parent_run_id = (
            self.agent.last_run.run_id if self.agent.last_run is not None else None
        )
        await self.agent.emit_event(
            CoreEvent(
                type="session_compaction_start",
                run_id=parent_run_id,
                data={
                    "operation_id": operation.operation_id,
                    "context_tokens": context_tokens,
                    "context_window": context_window,
                    "reason": "threshold",
                },
            )
        )
        try:
            async with asyncio.timeout(120):
                summary, cut = await compact_session(
                    messages,
                    operation,
                    settings,
                    previous_summary=previous_summary,
                )
        except Exception as exc:
            await self.agent.emit_event(
                CoreEvent(
                    type="session_compaction_error",
                    run_id=parent_run_id,
                    data={
                        "operation_id": operation.operation_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            )
            _logger.warning("Compaction summary generation failed; skipping")
            return
        finally:
            operation.signal_controller.signal._mark_notified()
            self._internal_abort_controller = None

        # Insert a CompactEntry into the session.
        if self.session.leaf_id is not None:
            try:
                self.session.compact(
                    up_to_entry_id=self.session.leaf_id,
                    summary=summary,
                    compacted_ids=list(
                        self.session.message_source_ids[: cut.first_kept_index]
                    ),
                )
                _logger.info("Compaction entry written, tokens before=%d", context_tokens)
            except Exception:
                _logger.warning("Failed to persist compaction entry", exc_info=True)
                await self.agent.emit_event(
                    CoreEvent(
                        type="session_compaction_error",
                        run_id=parent_run_id,
                        data={
                            "operation_id": operation.operation_id,
                            "error": "Compaction entry persistence failed",
                        },
                    )
                )
                return

        # Apply compaction to the Agent's in-memory context so future turns
        # use the compacted view.
        assembled = assemble_context(
            messages,
            compact_summary=summary,
            first_kept_index=cut.first_kept_index,
        )
        # Keep system message(s) and replace the rest
        system_msgs = [m for m in self.agent.messages if m.role == "system"]
        self.agent.messages = system_msgs + list(assembled)
        await self.agent.emit_event(
            CoreEvent(
                type="session_compaction_end",
                run_id=parent_run_id,
                data={
                    "operation_id": operation.operation_id,
                    "context_tokens": context_tokens,
                    "first_kept_index": cut.first_kept_index,
                },
            )
        )

    @staticmethod
    def _policy_evaluation_data(
        context: PolicyContext,
        evaluation: PolicyEvaluation,
    ) -> dict[str, Any]:
        return {
            "hook": context.hook,
            "input": {
                "tool_call": context.tool_call,
                "arguments": context.arguments,
                "tool_result": context.tool_result,
                "error": context.error,
                "error_info": context.error_info,
                "aborted": context.aborted,
                "metadata": context.metadata,
            },
            "final": evaluation.final,
            "decisions": evaluation.decisions,
        }

    @staticmethod
    def _raise_if_blocked(evaluation: PolicyEvaluation, subject: str) -> None:
        if evaluation.final.action in {"block", "require_confirmation"}:
            raise PolicyBlockedError(evaluation.final.reason or f"{subject} was blocked")


__all__ = ["BaseHarness", "PolicyBlockedError"]
