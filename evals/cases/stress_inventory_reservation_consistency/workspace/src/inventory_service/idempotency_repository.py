from __future__ import annotations

from .errors import IdempotencyConflict
from .models import IdempotencyRecord


class IdempotencyRepository:
    """Repository for durable idempotency-key bindings."""

    def __init__(self):
        self._records: dict[str, IdempotencyRecord] = {}

    def get(self, key: str) -> IdempotencyRecord | None:
        return self._records.get(key)

    def add(self, record: IdempotencyRecord) -> None:
        existing = self._records.get(record.key)
        if existing is not None and existing != record:
            raise IdempotencyConflict(record.key)
        self._records[record.key] = record

    def count(self) -> int:
        return len(self._records)

    def all(self) -> tuple[IdempotencyRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))
