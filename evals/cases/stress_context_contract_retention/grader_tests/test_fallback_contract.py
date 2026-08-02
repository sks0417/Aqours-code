import pytest

from notification_dispatcher import (DeliveryFailed, ProviderUnavailable,
                                     ScriptedProvider, build_application)


def test_disabled_primary_and_fallback_are_never_called(payload):
    email, sms, push = (ScriptedProvider("email"), ScriptedProvider("sms"),
                        ScriptedProvider("push"))
    app = build_application({"email": email, "sms": sms, "push": push},
                            disabled_channels={"email", "sms"})
    result = app.api.send(payload, idempotency_key="disabled")
    assert result["channel"] == "push"
    assert email.calls == [] and sms.calls == []


def test_missing_and_unavailable_channels_are_attempted_in_order(payload):
    email = ScriptedProvider(ProviderUnavailable("down"))
    app = build_application({"email": email})
    with pytest.raises(DeliveryFailed) as caught:
        app.api.send(payload, idempotency_key="unavailable")
    assert caught.value.attempted_channels == ("email", "sms", "push")


def test_duplicate_fallback_order_is_stable(payload):
    payload["fallback_channels"] = [" SMS ", "email", "sms", "push"]
    email = ScriptedProvider(ProviderUnavailable("down"))
    sms = ScriptedProvider("ok")
    app = build_application({"email": email, "sms": sms})
    assert app.api.send(payload, idempotency_key="order")["channel"] == "sms"
    assert len(sms.calls) == 1
