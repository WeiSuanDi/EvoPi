"""First-run model provider setup for the Coding CLI product."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from evopi.ai.models import ModelEnvironmentConfig, model_from_config
from evopi.cli.model_configuration import ResolvedCliModelConfiguration
from evopi.configuration import (
    CredentialRecord,
    CredentialStore,
    ModelProfile,
    PermissionHardener,
    UserConfig,
    UserConfigStore,
)
from evopi.core.context import AgentContext
from evopi.core.messages import UserMessage
from evopi.core.stream import ModelComplete

_PROVIDERS = {"anthropic", "openai-responses", "openai-compatible"}
_DEFAULT_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai-responses": "https://api.openai.com/v1",
    "openai-compatible": "https://api.openai.com/v1",
}


@dataclass(slots=True, frozen=True, kw_only=True)
class SetupOptions:
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_stdin: bool = False
    skip_test: bool = False


ConnectionTester = Callable[[ResolvedCliModelConfiguration], None]


def build_setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evopi setup",
        description="Configure a model provider for the EvoPi Coding CLI",
    )
    parser.add_argument("--provider", choices=sorted(_PROVIDERS))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="Read exactly one API key line from stdin",
    )
    parser.add_argument("--skip-test", action="store_true")
    return parser


def _default_connection_tester(resolved: ResolvedCliModelConfiguration) -> None:
    async def test() -> None:
        model = model_from_config(
            resolved.safe,
            api_key=resolved.api_key,
            timeout=30.0,
            max_tokens=16,
        )
        completed = False
        async for event in model.stream(
            AgentContext(messages=[UserMessage(content="Reply with OK.")], tools=[])
        ):
            completed = completed or isinstance(event, ModelComplete)
        if not completed:
            raise RuntimeError("provider stream ended without a valid completion")

    asyncio.run(test())


def _read_required(
    current: str | None,
    label: str,
    *,
    interactive: bool,
    stdin: TextIO,
    stderr: TextIO,
) -> str | None:
    if current:
        return current.strip()
    if not interactive:
        print(f"EvoPi setup requires {label} in non-interactive mode.", file=stderr)
        return None
    print(f"{label}: ", end="", file=stderr, flush=True)
    value = stdin.readline().strip()
    return value or None


def run_setup(
    options: SetupOptions,
    *,
    home: Path | None = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    interactive: bool | None = None,
    permission_hardener: PermissionHardener | None = None,
    connection_tester: ConnectionTester = _default_connection_tester,
) -> int:
    """Configure one default profile without exposing its credential."""

    is_interactive = stdin.isatty() if interactive is None else interactive
    provider = options.provider
    if provider is None and is_interactive:
        print(
            "Provider [anthropic/openai-responses/openai-compatible]: ",
            end="",
            file=stderr,
            flush=True,
        )
        provider = stdin.readline().strip()
    if provider not in _PROVIDERS:
        print("EvoPi setup requires a supported --provider.", file=stderr)
        return 2
    model = _read_required(
        options.model,
        "model name",
        interactive=is_interactive,
        stdin=stdin,
        stderr=stderr,
    )
    if model is None:
        return 2
    base_url = (options.base_url or _DEFAULT_URLS[provider]).rstrip("/")
    if options.api_key_stdin:
        api_key = stdin.readline().rstrip("\r\n")
    elif is_interactive:
        api_key = getpass.getpass("API key: ")
    else:
        print("Use --api-key-stdin in non-interactive mode.", file=stderr)
        return 2
    if not api_key:
        print("API key cannot be empty.", file=stderr)
        return 2

    safe = ResolvedCliModelConfiguration(
        safe=ModelEnvironmentConfig(
            provider=provider,
            model=model,
            base_url=base_url,
            credential_configured=True,
        ),
        api_key=api_key,
        profile="default",
        verified=not options.skip_test,
        sources={
            "provider": "setup",
            "model": "setup",
            "base_url": "setup",
            "credential": "setup",
        },
    )
    verified = False
    if not options.skip_test:
        while True:
            try:
                connection_tester(safe)
                verified = True
                break
            except Exception as exc:
                safe_message = str(exc).replace(api_key, "[redacted]")[:500]
                print(
                    f"Connection test failed: {type(exc).__name__}: {safe_message}",
                    file=stderr,
                )
                if not is_interactive:
                    return 1
                print(
                    "Retry, edit all values, save unverified, or cancel? [r/e/s/N]: ",
                    end="",
                    file=stderr,
                    flush=True,
                )
                action = stdin.readline().strip().lower()
                if action in {"r", "retry"}:
                    continue
                if action in {"e", "edit"}:
                    return run_setup(
                        SetupOptions(),
                        home=home,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        interactive=True,
                        permission_hardener=permission_hardener,
                        connection_tester=connection_tester,
                    )
                if action in {"s", "save"}:
                    break
                return 2

    target_home = home or Path.home() / ".evopi"
    profile = ModelProfile(
        name="default",
        provider=provider,
        model=model,
        base_url=base_url,
        verified=verified,
    )
    credential_store = CredentialStore(
        target_home / "credentials.json",
        **({"permission_hardener": permission_hardener} if permission_hardener else {}),
    )
    config_store = UserConfigStore(target_home / "config.toml")
    previous_credentials = credential_store.load()
    credential_store.save(
        (
            CredentialRecord(
                profile="default",
                provider=provider,
                base_url=base_url,
                api_key=api_key,
            ),
        )
    )
    try:
        config_store.save(UserConfig(active_profile="default", profiles=(profile,)))
    except BaseException:
        credential_store.save(previous_credentials)
        raise
    print(f"Configured profile 'default' for {provider}:{model}.", file=stdout)
    return 0


def setup_main(argv: list[str]) -> int:
    try:
        args = build_setup_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return run_setup(
        SetupOptions(
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            api_key_stdin=args.api_key_stdin,
            skip_test=args.skip_test,
        )
    )


__all__ = ["SetupOptions", "build_setup_parser", "run_setup", "setup_main"]
