# EvoPi Local JSONL RPC v2

RPC v2 is EvoPi's typed, same-machine stdio integration protocol. It exposes the existing
Harness execution path; it is not a second Tool, Policy, Confirmation, or authorization path.

## Transport and state machine

- UTF-8 JSON Lines over stdin/stdout; exactly one JSON object per line.
- Every envelope has `schema_version: 2`. Duplicate JSON keys, unknown envelope fields, NaN,
  invalid UTC timestamps, and non-JSON-safe values are rejected.
- The first non-empty request locks the connection to v1 or v2. A v2 connection must call
  `initialize` first. No lifecycle Event is sent before initialization succeeds.
- One Host has one bounded in-memory Event Stream and at most one active Run.
- `agent_end` is the only authoritative Run completion signal.
- Replay only covers the current Host process. Session and Trace remain the cross-process facts.

RPC v1 remains available as a legacy compatibility protocol for one formal release. The typed
Python client requires v2 and never silently downgrades.

## Canonical envelopes

Request:

```json
{"request_id":"req-1","method":"initialize","params":{"client_name":"desktop","client_version":"1.0"},"schema_version":2}
```

Successful response:

```json
{"request_id":"req-1","ok":true,"result":{"protocol":"evopi.rpc.v2","schema_version":2,"host_id":"HOST_UUID","session_id":"SESSION_ID","stream":{"stream_id":"STREAM_UUID","cursor":0,"oldest_sequence":0,"latest_sequence":0,"capacity":1000},"active_tool_names":[],"policy_names":[],"capabilities":{"event_replay":true,"confirmation":true,"text_steering":true,"text_follow_up":true},"steering_mode":"one-at-a-time","follow_up_mode":"one-at-a-time"},"error":null,"schema_version":2}
```

Error response:

```json
{"request_id":"req-2","ok":false,"result":null,"error":{"code":"not_initialized","message":"initialize must be the first request","details":{}},"schema_version":2}
```

Event:

```json
{"event_id":"EVENT_UUID","stream_id":"STREAM_UUID","sequence":1,"type":"agent_start","data":{"max_turns":20},"run_id":"RUN_ID","created_at":"2026-08-11T00:00:00+00:00","schema_version":2}
```

## Fixed methods

| Method | Exact parameters | Result purpose |
|---|---|---|
| `initialize` | `client_name`, `client_version` | Host, Session, Stream, tools, Policies, interaction modes, capabilities |
| `runtime.status` | empty object | Active Run, lifecycle, pending counts, last outcome |
| `run.start` | `prompt` | `run_id`, correlated `agent_start` sequence |
| `run.steer` | `run_id`, `content` | Durable interaction receipt |
| `run.follow_up` | `run_id`, `content` | Durable interaction receipt |
| `run.abort` | `run_id` | Correlated abort acknowledgement |
| `confirmation.list` | empty object | Strict pending Confirmation records |
| `confirmation.respond` | answer plus `expected_revision` | New status and revision |
| `confirmation.respond_batch` | answer array | Atomic acknowledgements |
| `events.replay` | `stream_id`, `after_sequence` | Requested cursor, retained window, snapshot boundary, ordered Events |
| `shutdown` | empty object | Graceful Host shutdown acknowledgement |

Interaction and Abort requests are capability-bound to their `run_id`. A handle from an earlier
Run cannot affect a later Run. Confirmation responses use optimistic revision checks; stale,
resolved, expired, or unknown requests never overwrite the durable decision.

## Replay cursor

An Event cursor is the pair `(stream_id, sequence)`. The replay response reports the requested
cursor, current oldest retained sequence, snapshot latest sequence, and Events in strict order.
The Host rejects another stream's cursor, a future cursor, and a cursor older than retained
history. Clients must subscribe to live delivery without losing or duplicating the Replay boundary.

## Stable errors

Protocol errors use stable codes including `not_initialized`, `already_initialized`,
`invalid_request`, `invalid_params`, `method_not_found`, `duplicate_request`, `run_mismatch`,
`run_already_active`, `run_not_active`, `event_stream_mismatch`, `event_cursor_invalid`,
`event_cursor_expired`, `stale_revision`, `duplicate_response`, `unknown_request`, `expired`,
`orphaned`, `host_closed`, `connection_closed`, and redacted `internal_error`.

Messages are safe summaries. Callers must branch on codes, not message text.

## Typed Event families

The Python client maps stable lifecycle names into `RpcRunEvent`, `RpcTurnEvent`,
`RpcMessageEvent`, `RpcToolExecutionEvent`, `RpcConfirmationEvent`, `RpcInteractionEvent`, and
`RpcErrorEvent`. Unknown Core, Harness, or Plugin event names become `RpcUnknownEvent` with their
validated JSON-safe data preserved. An unknown extension therefore remains observable without
weakening envelope, cursor, ordering, or Run correlation checks. `RpcRunHandle.wait()` accepts only
the matching Run's `agent_end` and validates its reason, Turn counts, committed messages, optional
error information, and terminal cursor before constructing `RpcRunResult`.

## Python client

```python
from evopi.rpc import EvoPiRpcClient

client = await EvoPiRpcClient.spawn()
run = await client.start_run("Summarize README.md")
async for event in run.events():
    ...
result = await run.wait()
await client.aclose()
```

`connect(reader, writer)` accepts caller-owned async text transports; `spawn()` owns the official
`evopi rpc` subprocess. The client has one physical read loop, replay/live continuity checking,
independent Event consumers, optional FIFO Confirmation handling, bounded stderr diagnostics, and
graceful shutdown with forced subprocess cleanup as a fallback. It never reconnects or replays a
side-effecting request automatically.

## Trust boundary

RPC v2 has no network listener, authentication, TLS, multi-tenant isolation, or remote
authorization. Do not expose it across a trust boundary without a separate transport and trust
layer. RPC callers can only operate the public Harness surface and pending Broker records; all
Tool calls still traverse the normal Policy, Confirmation, Trace, Abort, and Deadline chain.

The protocol-only checks in `tests/rpc_v2_conformance/` can be run separately from EvoPi's
Harness integration tests.
