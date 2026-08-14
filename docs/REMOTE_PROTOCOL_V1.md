# EvoPi Remote Protocol v1

Remote Protocol v1 exposes a single trusted EvoPi host over WebSocket without creating a second
Agent or Tool execution path. After device authentication, ordinary operations use the exact RPC
v2 envelopes documented in [RPC_V2_PROTOCOL.md](RPC_V2_PROTOCOL.md).

## Transport

- Endpoint: `wss://HOST/v1/connect`
- WebSocket subprotocol: `evopi.remote.v1`
- Text frames only; compression is disabled.
- Inbound frames are limited to 128 KiB. Responses and events are limited to 1 MiB.
- Direct non-loopback listeners require TLS 1.2 or newer. A loopback listener may be placed behind
  a trusted TLS reverse proxy.
- `Host` is always checked. Browser `Origin` is accepted only when it exactly matches the configured
  allowlist. Non-browser clients omit `Origin`.

Remote control frames use this strict envelope:

```json
{"schema_version":1,"type":"auth.begin","request_id":"req-1","data":{}}
```

Schema versions must be actual integers rather than JSON booleans. Unknown top-level fields,
duplicate JSON keys, non-finite numbers, binary frames, and malformed values are rejected.

## Device authentication

Devices use P-256 public keys represented as strict JWK. The private key remains on the device.
Signatures are SHA-256 ECDSA encoded as fixed 64-byte `r || s` values.

1. Client sends `auth.begin` with `device_id`.
2. Host returns `auth.challenge` containing a 32-byte nonce and a 30-second expiry.
3. Client signs the canonical challenge, which binds protocol, Host, Device, connection, nonce,
   issue time, and expiry.
4. Client sends `auth.complete`; Host returns `auth.ok` with the current scopes.

Challenges are single-use. A signature cannot be replayed on another connection or Host.

Pairing uses `pairing.submit` with a local, 12-character, single-use code, device name, and public
JWK. This creates only a pending request. Approval and scope assignment are possible only through
the authenticated local management IPC.

## Authorization and control lease

Scopes are `observe`, `control`, and `confirm`. `control` includes `observe`; `confirm` is independent.

- `observe`: initialize RPC, inspect runtime status, and consume events.
- `control`: acquire a lease and call `run.start`, `run.steer`, `run.follow_up`, or `run.abort`.
- `confirm`: list and answer pending confirmations with the observed revision.

Only one connection holds the control lease. It expires after 30 seconds and should be renewed every
10 seconds. Disconnecting does not abort an active Run. Remote `shutdown` is always forbidden.
Policy `block` cannot be overridden by any remote scope or response.

## Replay and live events

RPC v2 cursors bind `stream_id` and `sequence`. `events.page` provides bounded recovery pages with at
most 100 events and at most 1 MiB encoded size. A client replays from its last acknowledged cursor,
then consumes live RPC v2 events. Wrong-stream, future, expired, or gapped cursors fail explicitly.
The stream lasts only for one Gateway process; Session and Trace remain the cross-process facts.

## Failure and retry rules

Clients may reconnect, authenticate again, and resume observation from a valid cursor. They must not
automatically resend operations with side effects: Run start, steering, follow-up, abort, confirmation
responses, or lease mutations. If the transport fails after one of these writes, its outcome is
unknown and must be queried before a human or application decides what to do.

Closing the Python Remote Client never sends the locally meaningful RPC `shutdown` method. Pending
Remote control requests are completed with an outcome-unknown error instead of being left waiting
after the transport reader stops.

## Security boundary

Remote Gateway is a single-user, single-workspace control surface for a trusted local Agent. It is not
a multi-tenant service or an OS sandbox. `observe` can reveal sensitive model and Tool events, while
`confirm` can authorize high-risk actions. Production exposure should use a reverse proxy, WAF, or
tunnel for volumetric protection. Remote Audit stores a hash-chained redacted record; raw client IPs
are isolated in protected sidecars and removed after 30 days. Sensitive-key rejection traverses the
entire JSON container tree, and each locked append refreshes a chain head changed by another writer.
Host configuration, device identity, pairing state, and Audit verification use the same
duplicate-key and non-finite-number rejection. Challenge, lease, and persisted security timestamps
must carry an explicit UTC offset. Pairing-state recovery also revalidates exact record fields,
canonical identifiers and scopes, positive revisions, timestamp order, and the P-256
JWK-to-fingerprint binding before any device can authenticate. Restored pending identities remain
unique, and every approved request must match exactly one durable device record.

Canonical frames used by all clients are in `tests/conformance/remote_v1/`.
