"""Notification port definitions; ingestion does not deliver side effects."""


class NotificationSink:
    def publish(self, _receipt: dict) -> None:
        return None
