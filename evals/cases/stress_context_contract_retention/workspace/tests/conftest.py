def payload(identifier="n-1", recipient="user@example.test"):
    return {
        "notification_id": identifier,
        "recipient": recipient,
        "message": "Your report is ready",
        "primary_channel": "email",
        "fallback_channels": ["sms"],
    }
