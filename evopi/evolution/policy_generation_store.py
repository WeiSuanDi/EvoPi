"""Immutable persistence for Policy Generation records."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from evopi.evolution.file_lock import EvolutionFileLock
from evopi.evolution.policy_generation_protocol import (
    PolicyGenerationError,
    PolicyGenerationRecord,
    policy_generation_record_from_dict,
)


class PolicyGenerationStore:
    """Atomically persist digest-bound immutable Generation records.

    Records are rooted at ``EVOPI_HOME/generations/policies/records/``.
    IDs are immutable — writing an existing ID with different content is
    rejected.  Read-back verification rejects tampered files.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    @property
    def lock_path(self) -> Path:
        return self.root / ".store.lock"

    def record_path(self, generation_id: str) -> Path:
        normalized = generation_id.lower()
        if len(normalized) != 32 or any(
            char not in "0123456789abcdef" for char in normalized
        ):
            raise PolicyGenerationError(
                "generation_id must be a 32-character hexadecimal string",
                code="invalid_record",
            )
        return self.root / "records" / f"{normalized}.json"

    def save(self, record: PolicyGenerationRecord) -> PolicyGenerationRecord:
        """Persist one immutable Generation record.

        Raises when the ID already exists with different content.
        """
        payload = record.to_dict()
        stored = policy_generation_record_from_dict(payload)
        path = self.record_path(stored.generation_id)
        with EvolutionFileLock(self.lock_path):
            if path.exists():
                loaded = self.load(stored.generation_id)
                if loaded != stored:
                    raise PolicyGenerationError(
                        "Generation record already exists with different content",
                        code="record_conflict",
                    )
                return loaded
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    json.dump(
                        payload,
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            except OSError as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise PolicyGenerationError(
                    f"could not persist Generation record: {exc}",
                    code="store_io_error",
                ) from exc
        return self.load(stored.generation_id)

    def load(self, generation_id: str) -> PolicyGenerationRecord:
        """Load and strictly verify one stored Generation record."""
        path = self.record_path(generation_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PolicyGenerationError(
                f"could not read Generation record: {exc}",
                code="store_io_error",
            ) from exc
        return policy_generation_record_from_dict(payload)

    def exists(self, generation_id: str) -> bool:
        return self.record_path(generation_id).exists()

    def list_ids(self) -> tuple[str, ...]:
        records = self.root / "records"
        if not records.is_dir():
            return ()
        return tuple(
            sorted(
                item.stem
                for item in records.iterdir()
                if item.is_file() and item.suffix == ".json"
            )
        )


__all__ = ["PolicyGenerationStore"]
