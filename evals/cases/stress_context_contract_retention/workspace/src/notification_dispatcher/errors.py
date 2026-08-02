class NotificationError(Exception):
    """Base class for dispatcher errors."""


class ValidationError(NotificationError):
    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


class ProviderError(NotificationError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class PermanentProviderError(ProviderError):
    pass


class RateLimitExceeded(NotificationError):
    pass


class IdempotencyConflict(NotificationError):
    pass


class DeliveryFailed(NotificationError):
    def __init__(self, attempted_channels):
        self.attempted_channels = tuple(attempted_channels)
        super().__init__(f"delivery failed via {self.attempted_channels}")
