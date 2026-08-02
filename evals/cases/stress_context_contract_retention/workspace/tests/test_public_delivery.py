from notification_dispatcher import ProviderUnavailable, ScriptedProvider, build_application

def payload(identifier="n-1", recipient="user@example.test"):
    return {
        "notification_id": identifier, "recipient": recipient,
        "message": "Your report is ready", "primary_channel": "email",
        "fallback_channels": ["sms"],
    }


def test_primary_delivery_has_stable_receipt_shape():
    email = ScriptedProvider("mail-42")
    app = build_application({"email": email})
    result = app.api.send(payload(), idempotency_key="request-1")
    assert result == {
        "notification_id": "n-1", "recipient": "user@example.test",
        "channel": "email", "provider_id": "mail-42", "status": "delivered",
    }


def test_temporary_failure_uses_fallback():
    email = ScriptedProvider(ProviderUnavailable("maintenance"))
    sms = ScriptedProvider("sms-7")
    app = build_application({"email": email, "sms": sms})
    assert app.api.send(payload(), idempotency_key="request-2")["channel"] == "sms"
