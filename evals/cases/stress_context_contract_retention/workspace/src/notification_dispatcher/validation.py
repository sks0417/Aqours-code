from collections.abc import Mapping

from .errors import ValidationError
from .models import NotificationRequest


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("must be a non-empty string", field=field)
    return value.strip()


def validate_request(payload) -> NotificationRequest:
    if not isinstance(payload, Mapping):
        raise ValidationError("must be a mapping", field="payload")
    fallback = payload.get("fallback_channels", [])
    if not isinstance(fallback, list):
        raise ValidationError("must be a list", field="fallback_channels")
    normalized = []
    for index, value in enumerate(fallback):
        channel = _text(value, f"fallback_channels[{index}]").lower()
        if channel not in normalized:
            normalized.append(channel)
    return NotificationRequest(
        notification_id=_text(payload.get("notification_id"), "notification_id"),
        recipient=_text(payload.get("recipient"), "recipient"),
        message=_text(payload.get("message"), "message"),
        primary_channel=_text(payload.get("primary_channel"), "primary_channel").lower(),
        fallback_channels=tuple(normalized),
    )
