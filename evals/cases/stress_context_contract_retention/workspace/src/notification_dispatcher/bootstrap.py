from collections.abc import Mapping

from .api import NotificationAPI
from .dedupe import DedupeRepository
from .errors import ValidationError
from .policy import ChannelPolicy
from .providers import ProviderRegistry
from .rate_limit import RecipientRateLimiter
from .service import NotificationService


class NotificationApplication:
    def __init__(self, api, dedupe, limiter):
        self.api = api
        self.dedupe = dedupe
        self.limiter = limiter


def build_application(providers, *, disabled_channels=(), quota_per_recipient=10):
    if not isinstance(providers, Mapping):
        raise ValidationError("providers must be a mapping", field="providers")
    if (isinstance(quota_per_recipient, bool)
            or not isinstance(quota_per_recipient, int)
            or quota_per_recipient <= 0):
        raise ValidationError("quota must be a positive integer", field="quota_per_recipient")
    normalized = {}
    for raw_channel, provider in providers.items():
        if not isinstance(raw_channel, str) or not raw_channel.strip():
            raise ValidationError("invalid channel", field="providers")
        channel = raw_channel.strip().lower()
        if channel in normalized:
            raise ValidationError("duplicate normalized channel", field="providers")
        normalized[channel] = provider
    dedupe = DedupeRepository()
    limiter = RecipientRateLimiter(quota_per_recipient)
    service = NotificationService(
        ProviderRegistry(normalized), ChannelPolicy(disabled_channels),
        dedupe, limiter,
    )
    return NotificationApplication(NotificationAPI(service), dedupe, limiter)
