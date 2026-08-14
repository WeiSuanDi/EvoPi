# @evopi/remote-client

Browser-first ESM client for `evopi.remote.v1`. It uses Web Crypto P-256 device
keys and the same typed RPC v2 Run, Event, Confirmation, Steering, Follow-up,
and Abort protocol as the Python client.

Private keys should be stored as non-exportable `CryptoKey` objects in
IndexedDB. The client never uses cookies or bearer tokens and never retries
side-effecting requests after an unknown network outcome.
