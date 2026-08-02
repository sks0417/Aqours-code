import pytest

from notification_dispatcher import ScriptedProvider, ValidationError, build_application


def test_unknown_fields_and_normalized_channels_remain_compatible(payload):
    provider = ScriptedProvider("ok")
    payload["primary_channel"] = " EMAIL "
    app = build_application({" Email ": provider})
    assert app.api.send(payload, idempotency_key="future")["channel"] == "email"


@pytest.mark.parametrize("quota", [0, -1, 1.5, True, None])
def test_bad_quota_rejected(quota):
    with pytest.raises(ValidationError) as caught:
        build_application({}, quota_per_recipient=quota)
    assert caught.value.field == "quota_per_recipient"


def test_normalized_duplicate_provider_channels_rejected():
    with pytest.raises(ValidationError):
        build_application({"email": object(), " EMAIL ": object()})
