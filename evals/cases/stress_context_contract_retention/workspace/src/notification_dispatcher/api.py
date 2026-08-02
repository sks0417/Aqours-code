class NotificationAPI:
    def __init__(self, service):
        self._service = service

    def send(self, payload, *, idempotency_key):
        return self._service.send(payload, idempotency_key=idempotency_key)
