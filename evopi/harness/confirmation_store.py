"""ConfirmationStore implementations: in-memory default and file-backed.

The file store is an append-only JSONL fact log under a caller-supplied root.
It holds a cross-process lifetime lock, repairs only a torn final line, treats
middle corruption as fatal, persists each atomic batch as exactly one
``batch_transition`` fact with a single durable flush, and orphans pending
records of other runtimes when a crashed owner's lock is recovered.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from evopi.core.types import JsonObject
from evopi.harness.confirmation import (
    ConfirmationConflictError,
    ConfirmationDuplicateRequestError,
    ConfirmationError,
    ConfirmationExpiredError,
    ConfirmationFormatError,
    ConfirmationLockError,
    ConfirmationOrphanedError,
    ConfirmationRecord,
    ConfirmationResponse,
    ConfirmationStaleRevisionError,
    ConfirmationStatus,
    ConfirmationStoreClosedError,
    ConfirmationTransition,
    ConfirmationUnknownRequestError,
)
from evopi.harness.confirmation_codec import (
    decode_record,
    decode_transition,
    encode_record,
    encode_transition,
)

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"approved", "denied", "cancelled", "expired", "orphaned"}
)

_FACT_ENVELOPE_KEYS = frozenset({"schema_version", "fact", "data", "created_at"})
_BATCH_KEYS = frozenset({"transitions"})

_FACT_PATH_NAME = "confirmation_facts.jsonl"
_LOCK_PATH_NAME = "lock"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def already_resolved_error(record: ConfirmationRecord) -> ConfirmationConflictError:
    """Map a non-pending record to its structured conflict error."""
    request_id = record.request.id
    if record.status == "expired":
        return ConfirmationExpiredError(
            f"confirmation request {request_id!r} has expired",
            details={"request_id": request_id, "status": record.status},
        )
    if record.status == "orphaned":
        return ConfirmationOrphanedError(
            f"confirmation request {request_id!r} is orphaned",
            details={"request_id": request_id, "status": record.status},
        )
    return ConfirmationConflictError(
        f"confirmation request {request_id!r} is already {record.status}",
        details={"request_id": request_id, "status": record.status},
    )


def _terminal_status(status: ConfirmationStatus) -> None:
    if status not in _TERMINAL_STATUSES:
        raise ConfirmationConflictError(
            f"status {status!r} is not a terminal transition target",
            details={"status": status},
        )


def _validate_transition(
    record: ConfirmationRecord | None,
    transition: ConfirmationTransition,
) -> None:
    """Validate one optimistic transition against the current record."""
    _terminal_status(transition.status)
    if record is None:
        raise ConfirmationUnknownRequestError(
            f"no confirmation request {transition.request_id!r}",
            details={"request_id": transition.request_id},
        )
    if record.revision != transition.expected_revision:
        raise ConfirmationStaleRevisionError(
            f"request {transition.request_id!r} has revision {record.revision}, "
            f"expected {transition.expected_revision}",
            details={
                "request_id": transition.request_id,
                "expected_revision": transition.expected_revision,
                "current_revision": record.revision,
            },
        )
    if record.status != "pending":
        raise already_resolved_error(record)
    if (
        transition.response is not None
        and transition.response.request_id != transition.request_id
    ):
        raise ConfirmationConflictError(
            f"response {transition.response.request_id!r} does not correlate to "
            f"request {transition.request_id!r}",
            details={
                "request_id": transition.request_id,
                "response_request_id": transition.response.request_id,
            },
        )


def _apply_transition(
    record: ConfirmationRecord,
    *,
    status: ConfirmationStatus,
    response: ConfirmationResponse | None,
) -> ConfirmationRecord:
    return replace(
        record,
        status=status,
        response=response,
        revision=record.revision + 1,
        updated_at=_utc_now(),
    )


def _orphan_transitions(
    records: list[ConfirmationRecord],
) -> tuple[ConfirmationTransition, ...]:
    return tuple(
        ConfirmationTransition(
            request_id=record.request.id,
            expected_revision=record.revision,
            status="orphaned",
            response=None,
        )
        for record in records
    )


class InMemoryConfirmationStore:
    """Library-safe default: no persistence, no external resources."""

    def __init__(self) -> None:
        self._records: dict[str, ConfirmationRecord] = {}
        self._lock = threading.Lock()

    def create(self, record: ConfirmationRecord) -> None:
        with self._lock:
            if record.request.id in self._records:
                raise ConfirmationDuplicateRequestError(
                    f"confirmation request {record.request.id!r} already exists",
                    details={"request_id": record.request.id},
                )
            self._records[record.request.id] = record

    def get(self, request_id: str) -> ConfirmationRecord | None:
        with self._lock:
            return self._records.get(request_id)

    def list_pending(self) -> tuple[ConfirmationRecord, ...]:
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if record.status == "pending"
            )

    def transition(
        self,
        request_id: str,
        *,
        expected_revision: int,
        status: ConfirmationStatus,
        response: ConfirmationResponse | None,
    ) -> ConfirmationRecord:
        transition = ConfirmationTransition(
            request_id=request_id,
            expected_revision=expected_revision,
            status=status,
            response=response,
        )
        with self._lock:
            return self._apply_transitions_locked((transition,))[0]

    def transition_batch(
        self,
        transitions: tuple[ConfirmationTransition, ...],
    ) -> tuple[ConfirmationRecord, ...]:
        with self._lock:
            return self._apply_transitions_locked(tuple(transitions))

    def recover_orphans(self, *, runtime_id: str) -> tuple[ConfirmationRecord, ...]:
        with self._lock:
            pending = [
                record
                for record in self._records.values()
                if record.status == "pending" and record.runtime_id != runtime_id
            ]
            if not pending:
                return ()
            return self._apply_transitions_locked(_orphan_transitions(pending))

    def close(self) -> None:
        # In-memory state needs no release; kept for ConfirmationStore parity.
        return None

    def _apply_transitions_locked(
        self,
        transitions: tuple[ConfirmationTransition, ...],
    ) -> tuple[ConfirmationRecord, ...]:
        _validate_batch_uniqueness(transitions)
        for transition in transitions:
            _validate_transition(
                self._records.get(transition.request_id), transition
            )
        updated: list[ConfirmationRecord] = []
        for transition in transitions:
            record = self._records[transition.request_id]
            new_record = _apply_transition(
                record,
                status=transition.status,
                response=transition.response,
            )
            self._records[transition.request_id] = new_record
            updated.append(new_record)
        return tuple(updated)


def _validate_batch_uniqueness(
    transitions: tuple[ConfirmationTransition, ...],
) -> None:
    seen: set[str] = set()
    for transition in transitions:
        if transition.request_id in seen:
            raise ConfirmationDuplicateRequestError(
                f"duplicate request id {transition.request_id!r} in batch",
                details={"request_id": transition.request_id},
            )
        seen.add(transition.request_id)


class _CrossProcessLifetimeLock:
    """Exclusive cross-process lock held for the store's lifetime."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            self._lock_region(handle)
        except OSError as exc:
            handle.close()
            raise ConfirmationLockError(
                f"confirmation store is locked by another runtime at {self.path}",
                details={"path": str(self.path)},
            ) from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    @staticmethod
    def _lock_region(handle: BinaryIO) -> None:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class ConfirmationFileStore:
    """Persistent ConfirmationStore over an append-only JSONL fact log."""

    def __init__(self, root: str | Path, *, runtime_id: str | None = None) -> None:
        self.root = Path(root)
        self._runtime_id = runtime_id
        self._facts_path = self.root / _FACT_PATH_NAME
        self._records: dict[str, ConfirmationRecord] = {}
        self._repair_warnings: list[str] = []
        self._lock = threading.Lock()
        self._closed = False
        self._lifetime_lock = _CrossProcessLifetimeLock(self.root / _LOCK_PATH_NAME)
        try:
            self._lifetime_lock.acquire()
            self._facts_path.touch(exist_ok=True)
            self._private_permissions()
            self._replay()
            self._orphan_sweep()
        except Exception:
            self._lifetime_lock.release()
            raise

    @property
    def repair_warnings(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._repair_warnings)

    def create(self, record: ConfirmationRecord) -> None:
        with self._lock:
            self._check_not_closed()
            if record.request.id in self._records:
                raise ConfirmationDuplicateRequestError(
                    f"confirmation request {record.request.id!r} already exists",
                    details={"request_id": record.request.id},
                )
            self._append_fact(self._fact("create", encode_record(record)))
            self._records[record.request.id] = record

    def get(self, request_id: str) -> ConfirmationRecord | None:
        with self._lock:
            self._check_not_closed()
            return self._records.get(request_id)

    def list_pending(self) -> tuple[ConfirmationRecord, ...]:
        with self._lock:
            self._check_not_closed()
            return tuple(
                record
                for record in self._records.values()
                if record.status == "pending"
            )

    def transition(
        self,
        request_id: str,
        *,
        expected_revision: int,
        status: ConfirmationStatus,
        response: ConfirmationResponse | None,
    ) -> ConfirmationRecord:
        transition = ConfirmationTransition(
            request_id=request_id,
            expected_revision=expected_revision,
            status=status,
            response=response,
        )
        with self._lock:
            self._check_not_closed()
            record = self._records.get(request_id)
            _validate_transition(record, transition)
            assert record is not None
            self._append_fact(self._fact("transition", encode_transition(transition)))
            updated = _apply_transition(
                record, status=status, response=response
            )
            self._records[request_id] = updated
            return updated

    def transition_batch(
        self,
        transitions: tuple[ConfirmationTransition, ...],
    ) -> tuple[ConfirmationRecord, ...]:
        with self._lock:
            self._check_not_closed()
            return self._apply_transitions(tuple(transitions))

    def recover_orphans(self, *, runtime_id: str) -> tuple[ConfirmationRecord, ...]:
        with self._lock:
            self._check_not_closed()
            pending = [
                record
                for record in self._records.values()
                if record.status == "pending" and record.runtime_id != runtime_id
            ]
            if not pending:
                return ()
            return self._apply_transitions(_orphan_transitions(pending))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._lifetime_lock.release()

    def _check_not_closed(self) -> None:
        if self._closed:
            raise ConfirmationStoreClosedError(
                f"confirmation store at {self.root} is closed",
                details={"root": str(self.root)},
            )

    def _private_permissions(self) -> None:
        """Best-effort private permissions; failures are never fatal."""
        for path in (self.root, self._lifetime_lock.path, self._facts_path):
            with suppress(OSError):
                os.chmod(path, 0o600 if path.is_file() else 0o700)

    def _fact(self, kind: str, data: JsonObject) -> JsonObject:
        return {
            "schema_version": 1,
            "fact": kind,
            "data": data,
            "created_at": _utc_now().isoformat(),
        }

    def _append_fact(self, fact: JsonObject) -> None:
        line = json.dumps(fact, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._facts_path.open(
                "a", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ConfirmationError(
                f"failed to persist confirmation fact: {exc}",
                details={"path": str(self._facts_path)},
            ) from exc

    def _replay(self) -> None:
        raw = self._facts_path.read_text(encoding="utf-8")
        if raw == "":
            return
        lines = raw.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        facts: list[JsonObject] = []
        for index, line in enumerate(lines):
            try:
                facts.append(json.loads(line))
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    # Torn tail: a crash may leave only the final line partial.
                    self._repair_warnings.append(
                        f"repaired torn tail at line {index + 1}: {exc}"
                    )
                    continue
                raise ConfirmationFormatError(
                    f"corrupt confirmation fact at line {index + 1}",
                    details={"line": index + 1},
                ) from exc
        if self._repair_warnings:
            repaired = "\n".join(lines[:-1])
            self._facts_path.write_text(
                repaired + "\n" if repaired else "", encoding="utf-8"
            )
        for fact in facts:
            self._apply_fact(fact)

    def _apply_fact(self, fact: JsonObject) -> None:
        if not isinstance(fact, dict):
            raise ConfirmationFormatError("fact envelope must be an object")
        self._check_fact_envelope(fact)
        kind = fact["fact"]
        data = fact["data"]
        if kind == "create":
            record = decode_record(data)
            if record.request.id in self._records:
                raise ConfirmationFormatError(
                    f"duplicate create fact for {record.request.id!r}",
                    details={"request_id": record.request.id},
                )
            self._records[record.request.id] = record
            return
        if kind == "transition":
            self._apply_transition_fact(decode_transition(data))
            return
        if kind == "batch_transition":
            for transition in self._decode_batch_transitions(data):
                self._apply_transition_fact(transition)
            return
        raise ConfirmationFormatError(
            f"unknown fact type {kind!r}", details={"fact": kind}
        )

    def _apply_transition_fact(self, transition: ConfirmationTransition) -> None:
        record = self._records.get(transition.request_id)
        _validate_transition(record, transition)
        assert record is not None
        self._records[transition.request_id] = _apply_transition(
            record,
            status=transition.status,
            response=transition.response,
        )

    def _check_fact_envelope(self, fact: JsonObject) -> None:
        unknown = sorted(set(fact) - _FACT_ENVELOPE_KEYS)
        if unknown:
            raise ConfirmationFormatError(
                f"unknown field(s) in fact envelope: {', '.join(unknown)}",
                details={"unknown": unknown},
            )
        missing = sorted(_FACT_ENVELOPE_KEYS - set(fact))
        if missing:
            raise ConfirmationFormatError(
                f"missing field(s) in fact envelope: {', '.join(missing)}",
                details={"missing": missing},
            )
        version = fact.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ConfirmationFormatError(
                "fact 'schema_version' must be an integer",
                details={"schema_version": version},
            )
        if version != 1:
            raise ConfirmationFormatError(
                f"unsupported fact schema version {version!r}",
                details={"schema_version": version},
            )
        if not isinstance(fact["fact"], str):
            raise ConfirmationFormatError("fact 'fact' must be a string")
        created_at = fact.get("created_at")
        if not isinstance(created_at, str):
            raise ConfirmationFormatError(
                "fact 'created_at' must be an ISO-8601 datetime string"
            )
        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise ConfirmationFormatError(
                "fact 'created_at' is not a valid ISO-8601 datetime"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ConfirmationFormatError(
                "fact 'created_at' must carry a timezone offset"
            )

    def _decode_batch_transitions(
        self, data: Any
    ) -> tuple[ConfirmationTransition, ...]:
        if not isinstance(data, dict):
            raise ConfirmationFormatError("batch_transition data must be an object")
        unknown = sorted(set(data) - _BATCH_KEYS)
        if unknown:
            raise ConfirmationFormatError(
                f"unknown field(s) in batch_transition data: {', '.join(unknown)}",
                details={"unknown": unknown},
            )
        missing = sorted(_BATCH_KEYS - set(data))
        if missing:
            raise ConfirmationFormatError(
                f"missing field(s) in batch_transition data: {', '.join(missing)}",
                details={"missing": missing},
            )
        transitions = data["transitions"]
        if not isinstance(transitions, list):
            raise ConfirmationFormatError(
                "batch_transition 'transitions' must be an array"
            )
        return tuple(decode_transition(item) for item in transitions)

    def _orphan_sweep(self) -> None:
        pending = [
            record
            for record in self._records.values()
            if record.status == "pending" and record.runtime_id != self._runtime_id
        ]
        if not pending:
            return
        self._apply_transitions(_orphan_transitions(pending))

    def _apply_transitions(
        self,
        transitions: tuple[ConfirmationTransition, ...],
    ) -> tuple[ConfirmationRecord, ...]:
        _validate_batch_uniqueness(transitions)
        for transition in transitions:
            _validate_transition(
                self._records.get(transition.request_id), transition
            )
        if transitions:
            data: JsonObject = {
                "transitions": [encode_transition(t) for t in transitions]
            }
            self._append_fact(self._fact("batch_transition", data))
        updated: list[ConfirmationRecord] = []
        for transition in transitions:
            record = self._records[transition.request_id]
            new_record = _apply_transition(
                record,
                status=transition.status,
                response=transition.response,
            )
            self._records[transition.request_id] = new_record
            updated.append(new_record)
        return tuple(updated)


__all__ = [
    "ConfirmationFileStore",
    "InMemoryConfirmationStore",
    "already_resolved_error",
]
