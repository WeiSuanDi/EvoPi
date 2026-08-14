# @evopi/remote-client

Browser-first ESM client for `evopi.remote.v1`. It uses Web Crypto P-256 device
keys and the same typed RPC v2 Run, Event, Confirmation, Steering, Follow-up,
and Abort protocol as the Python client.

Private keys should be stored as non-exportable `CryptoKey` objects in
IndexedDB. The client never uses cookies or bearer tokens and never retries
side-effecting requests after an unknown network outcome.

The client supports device authentication, control leases, typed RPC v2 Run
operations, Confirmation, replay pages, and a resilient observation iterator.
Observation reconnects authenticate again and resume from the last cursor;
`run.start`, steering, follow-up, abort, Confirmation responses, and lease
mutations are never automatically replayed.
