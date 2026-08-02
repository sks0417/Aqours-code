from copy import deepcopy

from .errors import IdempotencyConflict


class DedupeRepository:
    def __init__(self):
        self._records = {}

    def lookup(self, key, fingerprint):
        record = self._records.get(key)
        if record is None:
            return None
        previous, receipt = record
        if previous != fingerprint:
            raise IdempotencyConflict("idempotency key reused with different request")
        return deepcopy(receipt)

    def remember(self, key, fingerprint, receipt):
        self._records[key] = (fingerprint, deepcopy(receipt))

    def count(self):
        return len(self._records)
