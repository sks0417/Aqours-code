import inspect

import notification_dispatcher as package
from notification_dispatcher.api import NotificationAPI


def test_public_surface_and_signatures():
    expected = {
        "NotificationApplication", "build_application", "ScriptedProvider",
        "NotificationError", "ValidationError", "ProviderError",
        "ProviderUnavailable", "PermanentProviderError", "RateLimitExceeded",
        "IdempotencyConflict", "DeliveryFailed",
    }
    assert expected <= set(package.__all__)
    assert str(inspect.signature(package.build_application)) == (
        "(providers, *, disabled_channels=(), quota_per_recipient=10)")
    assert str(inspect.signature(NotificationAPI.send)) == (
        "(self, payload, *, idempotency_key)")
    assert issubclass(package.ProviderUnavailable, package.ProviderError)
    assert issubclass(package.PermanentProviderError, package.ProviderError)
