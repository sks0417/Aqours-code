from collections import defaultdict

from .models import ImportResult
from .serialization import rows_fingerprint, serialize_result
from .validation import validate_rows


class InventoryImportService:
    def __init__(self, repository, dedupe):
        self.repository = repository
        self.dedupe = dedupe
        self._next_batch = 1

    def import_batch(self, payload, *, idempotency_key):
        rows = validate_rows(payload)
        fingerprint = rows_fingerprint(rows)
        # BUG: repository size must not be part of idempotency identity.
        scoped_key = f"{idempotency_key}:{self.repository.count()}"
        cached = self.dedupe.lookup(scoped_key, fingerprint)
        if cached is not None:
            return cached
        # BUG: duplicates inside this batch are silently overwritten.
        self.repository.insert_many(rows)
        result = serialize_result(ImportResult(
            f"batch-{self._next_batch:04d}", len(rows),
            tuple(row.external_id for row in rows),
        ))
        self._next_batch += 1
        self.dedupe.remember(scoped_key, fingerprint, result)
        return result
