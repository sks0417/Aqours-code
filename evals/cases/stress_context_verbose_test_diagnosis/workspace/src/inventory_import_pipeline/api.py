class InventoryImportAPI:
    def __init__(self, service):
        self._service = service

    def import_batch(self, payload, *, idempotency_key):
        return self._service.import_batch(payload, idempotency_key=idempotency_key)
