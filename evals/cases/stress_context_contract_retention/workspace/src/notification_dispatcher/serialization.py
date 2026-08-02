import hashlib
import json

from .models import DeliveryReceipt, NotificationRequest


def request_fingerprint(request: NotificationRequest) -> str:
    payload = {
        "notification_id": request.notification_id,
        "recipient": request.recipient,
        "message": request.message,
        "primary_channel": request.primary_channel,
        "fallback_channels": request.fallback_channels,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def serialize_receipt(receipt: DeliveryReceipt) -> dict:
    return {
        "notification_id": receipt.notification_id,
        "recipient": receipt.recipient,
        "channel": receipt.channel,
        "provider_id": receipt.provider_id,
        "status": receipt.status,
    }
