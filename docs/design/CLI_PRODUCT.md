# EvoPi CLI Product

## Product boundary

The CLI is the first product host for `CodingHarness`; it is not a second runtime and does not
change Core semantics. It exposes the same Harness, Policy, Session, Plugin, Trace, Retry,
Failover, and Confirmation contracts through two entry layers:

```text
Interactive workbench
├── evopi
└── evopi chat [INITIAL_PROMPT]

Management and automation
├── evopi run [PROMPT] [--json]
├── evopi session / policy / plugin
└── evopi config show / doctor
```

`evopi "PROMPT"` remains a compatibility alias for one-shot execution.

## Output contract

- Interactive rendering and one-shot model text use the existing Rich/streaming surfaces.
- In one-shot mode, model text is written to stdout; runtime status, warnings, retries,
  Confirmation, and Session information are written to stderr.
- `run --json` emits schema v1 with Session/Run IDs, end reason, `turns_used`, `max_turns`, final
  Assistant message, and a bounded safe error projection. It never copies the Prompt, Tool
  arguments, Provider State, or credentials.
- Normal and governed termination return `0`; runtime failure, deadline, or turn limit return
  `1`; user Abort returns `130`. A missing `run` input returns `2`.

## Effective runtime controls

Model routing accepts explicit ordered fallbacks and circuit settings. The CLI validates every
candidate before opening a Session or issuing a model request. Tool include/exclude options form
a Harness-level capability ceiling: Plugin overrides, Plan Mode, and SubAgents may narrow it but
cannot re-enable a disabled Tool.

`--max-turns N` and `EVOPI_MAX_TURNS` configure a strict model Turn budget with
`CLI > environment > 20` precedence. The value must be positive and has no artificial upper
cap. The REPL displays `Turn current/max`; `/status`, `/settings`, and `config show` expose the
effective limit.

`--shell auto|cmd|powershell` and `EVOPI_SHELL` use
`CLI > environment > auto` precedence. Resolution completes before Model or Session
construction. `auto` means `cmd.exe` on Windows and `/bin/sh` on POSIX; explicit PowerShell
prefers `pwsh` and falls back to `powershell.exe` only on Windows. Arbitrary executable paths
are not accepted. Config, Doctor, Startup, and `/settings` expose both requested and resolved
values without executing a command.

The public Harness snapshot exposes the registered and active Tool views, source, effects, and
Plugin ownership. Coding resource snapshots expose only Memory status/count, Skill identity and
risk, and SubAgent availability. REPL and diagnostics never inspect private Harness fields.

## Interactive workbench

The REPL has one typed command registry for built-in and Plugin commands. The registry is the
source of truth for routing, help, reserved names, and completion. Its status surfaces show
effective capabilities without copying Memory entries, Skill content, secrets, or raw Plugin
state. Modal Confirmation and Plugin UI interactions pause and resume the same Live display.

Session operations (`/new`, `/branch`, `/switch`, `/merge`, `/compact`) always call public Harness
APIs. `/reload` transactionally refreshes approved Plugin and Policy artifacts and retains the old
snapshot on failure.

## Dynamic Coding prompt

`CodingHarness` builds a compact System Prompt from the final active Tool view. Each Tool may
provide `metadata["prompt_snippet"]` and `metadata["prompt_guidelines"]`; conditional guidelines
are derived from normalized effects. Tool ceilings, Plugin overrides, reload, and Session leaf
state refresh that view.

The prompt states EvoPi's governance boundary: Policy may block, confirm, rewrite, or validate,
and the model cannot fabricate approval or treat reload as approval. Session commands are UI
knowledge and are not injected into the model. Plugin-authoring guidance appears only as a short
boundary for explicit extension requests.

When that request is explicit and the Tool ceiling permits writes, the active Tool view includes
`create_plugin_candidate`. Its dynamic guideline directs the model to use the packaged scaffold,
incremental edits, and candidate tests, then stop at human review/approval/reload. Read-only
ceilings and Plan Mode remove it automatically through its declared `write` effect.

`--system-prompt` fully replaces the generated prompt. `--append-system-prompt` appends to either
the generated or replacement prompt. Plugin Prompt Fragments and Skill context remain in the
per-model-call Context assembly chain.

When a Coding run reaches two remaining Turns, an ephemeral Context message requests
prioritization and final-answer preparation. On the last Turn, the model receives an empty Tool
view and a final-answer instruction. A Coding Policy blocks fabricated ToolCalls; no `N+1`
summary request is created. These are Domain behaviors, not defaults of bare Agent/BaseHarness.

## Configuration and diagnostics

`config show` resolves the effective Provider, model, Base URL, fallbacks, credential presence,
EvoPi home, Session root, Workspace Trust, Memory path, and Skill sources. It returns only a
boolean credential-presence field.

`doctor` is offline. It validates Python and workspace prerequisites, model configuration and URL,
credential presence, writable local stores, approved immutable Policy/Plugin artifacts, and Trust
state. It does not call a model, access the network, or import unapproved Plugin candidates.
Doctor schema v1 uses `failed > warning > passed`, mapped to exit codes `1 / 2 / 0`.

## Non-goals

The first version does not add a full-screen TUI, in-REPL model switching, settings mutation,
RPC/JSONL event streaming, direct shell syntax, Session deletion, or implicit authorization.
`BaseHarness` remains neutral and never reads CLI/user configuration unless explicitly wired by a
host.
