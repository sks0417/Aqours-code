from __future__ import annotations


class LedgerAPI:
    def __init__(self, service, recovery):
        self._service = service
        self._recovery = recovery

    def ingest(self, payload, *, idempotency_key):
        return self._service.ingest(payload, idempotency_key=idempotency_key)

    def get_balance(self, account_id):
        return self._service.get_balance(account_id)

    def get_partition_sequence(self, partition):
        return self._service.get_partition_sequence(partition)

    def create_checkpoint(self):
        return self._recovery.create_checkpoint()

    def recover(self):
        return self._recovery.recover()
