import pytest

from notification_dispatcher import (PermanentProviderError, RateLimitExceeded,
                                     ScriptedProvider, build_application)


def test_permanent_error_escapes_without_fallback(payload):
    email = ScriptedProvider(PermanentProviderError("rejected"))
    sms = ScriptedProvider("must-not-run")
    app = build_application({"email": email, "sms": sms})
    with pytest.raises(PermanentProviderError):
        app.api.send(payload, idempotency_key="permanent")
    assert sms.calls == []
    assert app.dedupe.count() == 0


def test_rate_limit_never_calls_or_records(payload):
    email = ScriptedProvider("first", "second")
    app = build_application({"email": email}, quota_per_recipient=1)
    app.api.send(payload, idempotency_key="first")
    payload["notification_id"] = "notice-2"
    with pytest.raises(RateLimitExceeded):
        app.api.send(payload, idempotency_key="second")
    assert len(email.calls) == 1
    assert app.dedupe.count() == 1


def test_disabled_only_failure_reports_no_attempts(payload):
    from notification_dispatcher import DeliveryFailed
    email = ScriptedProvider("never")
    app = build_application({"email": email},
                            disabled_channels={"email", "sms", "push"})
    with pytest.raises(DeliveryFailed) as caught:
        app.api.send(payload, idempotency_key="none")
    assert caught.value.attempted_channels == ()
