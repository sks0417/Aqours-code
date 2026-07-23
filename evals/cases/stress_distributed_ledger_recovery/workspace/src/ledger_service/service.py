from __future__ import annotations

from .fingerprint import request_fingerprint
from .models import Receipt
from .serialization import serialize_receipt
from .validation import normalize_events, normalize_key


class LedgerService:
    def __init__(self, event_store, balances, sequences, idempotency, receipts, receipt_ids):
        self._events = event_store
        self._balances = balances
        self._sequences = sequences
        self._idempotency = idempotency
        self._receipts = receipts
        self._receipt_ids = receipt_ids

    def ingest(self, payload, *, idempotency_key):
        key = normalize_key(idempotency_key)
        events = normalize_events(payload)
        fingerprint = request_fingerprint(events)
        existing = self._idempotency.resolve(key, fingerprint)
        if existing is not None:
            return serialize_receipt(existing)

        event_snapshot = self._events.snapshot()
        sequence_snapshot = self._sequences.snapshot()
        receipt_snapshot = self._receipts.snapshot()
        batch_id = self._receipt_ids.allocate()
        self._idempotency.bind(key, fingerprint, None)
        try:
            self._sequences.validate(events)
            self._events.append_many(events)
            self._balances.apply_many(events)
            self._sequences.advance(events)
            receipt = Receipt(
                batch_id=batch_id,
                event_ids=tuple(event.event_id for event in events),
                balances=tuple(sorted(self._balances.snapshot().items())),
                sequences=tuple(sorted(self._sequences.snapshot().items())),
            )
            self._receipts.add(receipt)
            self._idempotency.bind(key, fingerprint, receipt)
            return serialize_receipt(receipt)
        except Exception:
            self._events.restore(event_snapshot)
            self._sequences.restore(sequence_snapshot)
            self._receipts.restore(receipt_snapshot)
            raise

    def get_balance(self, account_id: str) -> dict:
        return dict(self._balances.balance(account_id))

    def get_partition_sequence(self, partition: str) -> int:
        return self._sequences.get(partition)
