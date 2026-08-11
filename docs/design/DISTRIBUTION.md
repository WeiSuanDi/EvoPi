# EvoPi Distribution Design

> Current public baseline: `v0.2.0`, released through the verified GitHub tag workflow on
> 2026-08-11.

## Product boundary

Distribution installs and selects an EvoPi executable. It is outside Core, Harness, Policy,
Session, Trace, and Plugin semantics. A managed install does not gain additional runtime
authorization; it only gains an updater-owned version layout.

## Official source and trust checks

Stable GitHub Releases from `WeiSuanDi/EvoPi` are the only v1 product source. Tags must match
`vMAJOR.MINOR.PATCH`, and draft or prerelease entries are rejected. The installer and updater
accept the exact versioned universal wheel and `SHA256SUMS`, require HTTPS GitHub asset hosts,
compare the wheel digest, and inspect `.dist-info/METADATA` for package name and version.

SHA-256 verifies the downloaded EvoPi artifact against the Release manifest; it is not code
signing and does not authenticate third-party dependencies downloaded by pip. GitHub build
provenance is published by the tag workflow as additional evidence.

## Managed runtime

```text
~/.evopi/
├── bin/evopi.cmd
└── runtime/
    ├── current.txt
    ├── update.lock
    └── versions/<version>/
```

The stable launcher sets `EVOPI_MANAGED_ROOT`, reads `current.txt`, and forwards arguments to the
selected runtime. Update work is serialized by a cross-process lock. A new version is built in a
staging directory, installs the Release wheel and dependencies, and passes import/version/help
smoke checks. Windows venv launchers embed their absolute directory, so construction happens in
the final, not-yet-referenced version path; failure removes it. A verification marker is written
before `current.txt` is atomically replaced. The current and previous versions are retained; old
cleanup failure is a warning.

Rollback is offline and accepts only a retained directory carrying a valid verification marker.
Unsupported package-manager installations return guidance rather than mutating their environment.

## Release authority

The tag workflow validates the tag against `pyproject.toml`, runs the full gate on Windows and
Linux with Python 3.11 through 3.13, builds wheel and sdist, validates package data, exercises the
Windows installer against local simulated assets, generates checksums and provenance, and only
then creates a Release. It also supports a manual non-publishing preflight on `main`; the publish
job is restricted to tag-push events. Creating or replacing a release tag remains a user-authorized
publishing action.
