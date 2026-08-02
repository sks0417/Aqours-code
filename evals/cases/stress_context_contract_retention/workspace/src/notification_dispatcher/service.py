from .errors import DeliveryFailed, ProviderError, ProviderUnavailable
from .models import DeliveryReceipt
from .serialization import request_fingerprint, serialize_receipt
from .validation import validate_request


class NotificationService:
    def __init__(self, providers, policy, dedupe, limiter):
        self.providers = providers
        self.policy = policy
        self.dedupe = dedupe
        self.limiter = limiter

    def send(self, payload, *, idempotency_key):
        request = validate_request(payload)
        fingerprint = request_fingerprint(request)
        cached = self.dedupe.lookup(idempotency_key, fingerprint)
        if cached is not None:
            return cached
        self.limiter.consume(request.recipient)
        attempted = []
        for channel in self.policy.candidates(request):
            provider = self.providers.get(channel)
            if provider is None:
                attempted.append(channel)
                continue
            attempted.append(channel)
            try:
                provider_id = provider.deliver(request)
            except ProviderError:  # BUG: permanent provider errors must escape.
                continue
            receipt = serialize_receipt(DeliveryReceipt(
                request.notification_id, request.recipient,
                channel, str(provider_id),
            ))
            self.dedupe.remember(idempotency_key, fingerprint, receipt)
            return receipt
        raise DeliveryFailed(attempted)
