from dataclasses import dataclass


@dataclass(frozen=True)
class NotificationRequest:
    notification_id: str
    recipient: str
    message: str
    primary_channel: str
    fallback_channels: tuple[str, ...]


@dataclass(frozen=True)
class DeliveryReceipt:
    notification_id: str
    recipient: str
    channel: str
    provider_id: str
    status: str = "delivered"
