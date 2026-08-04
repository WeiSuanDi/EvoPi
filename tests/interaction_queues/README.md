# SFU-3 — Independent Interaction Conformance Kit

An implementation-independent, executable conformance kit for the Steering and
Follow-up interaction-queue contract frozen in the `steering-follow-up-v1`
milestone `CONTEXT.md` sections 2-4.  It never imports production EvoPi code,
never touches the network or a real model, and never depends on wall-clock
time.  Integration provides the production adapter.

## Layout

| File | Role |
|---|---|
| `conformance.py` | Observable vocabulary: `InteractionAdapter` Protocol, frozen result dataclasses, scenario helpers, the 27-scenario battery, and `run_conformance(adapter_factory)` |
| `reference.py` | `ReferenceInteractionAdapter` — deterministic known-good virtual runtime used as the oracle |
| `mutants.py` | 8 deliberately broken adapters, one per acceptance-matrix defect |
| `test_reference_contracts.py` | Direct unit probes of the reference (limits, codecs, budget, gates) |
| `test_kit_validity.py` | Proof that the reference passes every scenario and each mutant fails exactly its target |
| `test_run_conformance.py` | Tests of the Integration entry point |

## Adapter obligations

Implement `conformance.InteractionAdapter` with default construction settings
(`steering_mode="one-at-a-time"`, `follow_up_mode="one-at-a-time"`,
`InteractionLimits()`).  The Protocol docstring is the contract; the
acceptance-relevant obligations are:

- **Admission is atomic and evidence-backed.**  `steer`/`follow_up` return a
  receipt or a structured `InteractionError` (`run_not_active`,
  `interaction_closed`, `interaction_queue_full`, `interaction_content_invalid`,
  `interaction_content_too_large`).  Every receipt must later be covered by
  exactly one `interaction_delivered` or `interaction_cleared` event before
  `agent_end`; a rejected admission creates no event.
- **Validation is strict.**  Content must be a string with non-whitespace
  text, its UTF-8 size must not exceed the limit, origins and modes are exact
  literals, limits are positive ints (booleans rejected), and the total
  pending count across both queues is capped.  Accepted content is preserved
  exactly (never trimmed).
- **Safe-point steering.**  Steering is delivered only after the current
  Assistant message commits, every sibling ToolCall reaches its final result
  (including Policy, Confirmation, error, and `after_tool_call`), and
  `turn_end` finishes — never mid-batch.  It may also be drained at the
  terminal candidate and at the initial admission safe point.
- **Terminal-only follow-up.**  Follow-up is delivered only when the Run
  would otherwise finish and no steering is pending; never during ordinary
  Tool continuation.
- **Mode semantics.**  `one-at-a-time` drains one item per safe point and
  lets its full continuation finish first; `all` drains one atomic FIFO
  snapshot before one model request.  Arrivals during the drain join the next
  batch.  FIFO order is preserved within each kind.
- **Terminal priority.**  Abort, deadline, final-retry error, Turn
  exhaustion, external cancellation, and graceful close clear every
  undelivered item fail closed with the matching reason; explicit queued
  input overrides a graceful `terminate=True` decision but never those
  terminal reasons.
- **Budget.**  Every delivered interaction's model request consumes one
  ordinary Turn; provider retries are Model Attempts only.
- **Boundaries.**  Only delivered input becomes a committed UserMessage (with
  the exact `interaction` metadata block); queued/cleared content never
  reaches the Session, and event data and Trace never duplicate content.
- **Observability.**  `events()` returns the ordered log, `snapshot()` an
  immutable point-in-time view, `turn_count()`/`attempt_count()` the budget
  accounting, and `wait_for_idle()` resolves only after queue settlement and
  awaited `agent_end` listeners.

## Integration handoff

1. Write an integration-owned test that builds the production adapter and
   runs the battery:

   ```python
   from tests.interaction_queues.conformance import run_conformance

   def test_production_adapter_is_conformant() -> None:
       results = run_conformance(ProductionAdapterFactory)
       assert all(status == "ok" for status in results.values()), results
   ```

   Lane files are never modified; the entry point takes a zero-argument
   factory and returns `{scenario_name: "ok" | failure_report}`.

2. Map `InteractionError` codes to the RPC safe error codes
   (`run_not_active`, `interaction_queue_full`, `interaction_content_invalid`,
   `interaction_content_too_large`, `interaction_closed`) and to the public
   exceptions named in `CONTEXT.md` section 3.
3. Keep the adapter production-import-free of this kit: the kit is a client,
   not a dependency of production code.

## Running the kit

```text
python -m pytest tests/interaction_queues -q
python -m ruff check tests/interaction_queues
python -m mypy tests/interaction_queues
```
