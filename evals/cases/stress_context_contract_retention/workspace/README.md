# Notification Dispatcher

This service selects a primary delivery channel and optional fallbacks while maintaining a stable synchronous Python API. The supported entry point is:

```python
app = build_application(providers, disabled_channels={"sms"}, quota_per_recipient=3)
receipt = app.api.send(payload, idempotency_key="tenant:request")
```

`providers` maps channel names to objects exposing `deliver(request)`. The payload contains non-empty `notification_id`, `recipient`, `message`, `primary_channel`, and an optional list `fallback_channels`. Unknown payload fields must be ignored for forward compatibility.

The API signature and exported exceptions are compatibility commitments. A successful receipt contains exactly `notification_id`, `recipient`, `channel`, `provider_id`, and `status`; status is the lowercase string `delivered`.

See `docs/delivery_contract.md` for channel selection and retry semantics and `docs/operations.md` for failure and quota boundaries. These documents are normative.

The public tests cover ordinary delivery. Production incidents have tended to combine multiple edge conditions, so do not assume passing them is sufficient.
