# RPC v2 conformance suite

This directory contains protocol-level tests that do not construct a CodingHarness or call a
model. It freezes the v2 envelope, mandatory initialization, fixed method names, cursor identity,
and canonical result validation independently from the ordinary unit tests under `tests/rpc/`.

Run it with:

```bash
python -m pytest -q tests/rpc_v2_conformance
```

An embedding Host should additionally run its own vertical tests proving that `run.start`,
Confirmation, Steering/Follow-up, Abort, and Event forwarding all use its normal governed Harness
path.
