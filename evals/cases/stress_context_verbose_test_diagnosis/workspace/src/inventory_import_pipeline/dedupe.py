from copy import deepcopy

from .errors import IdempotencyConflict


class ImportDedupeRepository:
    def __init__(self):
        self._records = {}

    def lookup(self, key, fingerprint):
        value = self._records.get(key)
        if value is None:
            return None
        previous, result = value
        if previous != fingerprint:
            raise IdempotencyConflict("idempotency key reused")
        return deepcopy(result)

    def remember(self, key, fingerprint, result):
        self._records[key] = (fingerprint, deepcopy(result))

    def count(self):
        return len(self._records)
