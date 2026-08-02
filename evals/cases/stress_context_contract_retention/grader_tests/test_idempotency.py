import pytest

from notification_dispatcher import IdempotencyConflict, ScriptedProvider, build_application


def test_retry_returns_detached_original_without_new_quota(payload):
    provider = ScriptedProvider("provider-1", "provider-2")
    app = build_application({"email": provider}, quota_per_recipient=1)
    first = app.api.send(payload, idempotency_key="same")
    first["provider_id"] = "mutated"
    again = app.api.send(dict(payload), idempotency_key="same")
    assert again["provider_id"] == "provider-1"
    assert len(provider.calls) == 1
    assert app.limiter.usage(payload["recipient"]) == 1


def test_same_key_different_normalized_request_conflicts(payload):
    provider = ScriptedProvider("one")
    app = build_application({"email": provider})
    app.api.send(payload, idempotency_key="conflict")
    payload["message"] = "different"
    with pytest.raises(IdempotencyConflict):
        app.api.send(payload, idempotency_key="conflict")


def test_failed_attempt_is_not_remembered(payload):
    from notification_dispatcher import DeliveryFailed, ProviderUnavailable
    provider = ScriptedProvider(ProviderUnavailable("once"), "later")
    app = build_application({"email": provider})
    payload["fallback_channels"] = []
    with pytest.raises(DeliveryFailed):
        app.api.send(payload, idempotency_key="retry")
    assert app.api.send(payload, idempotency_key="retry")["provider_id"] == "later"
