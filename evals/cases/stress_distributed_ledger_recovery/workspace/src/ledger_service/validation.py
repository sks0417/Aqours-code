from __future__ import annotations

import re
from collections.abc import Mapping

from .errors import ValidationError
from .models import LedgerEvent


_KEY = re.compile(r"^[A-Za-z0-9._:-]+$")
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string", field=field)
    return value.strip()


def normalize_key(value) -> str:
    key = _text(value, "idempotency_key")
    if len(key) > 128 or not _KEY.fullmatch(key):
        raise ValidationError("invalid idempotency key", field="idempotency_key")
    return key


def normalize_accounts(value) -> dict[str, dict]:
    if not isinstance(value, Mapping) or not value:
        raise ValidationError("initial_accounts must be a non-empty mapping", field="initial_accounts")
    result = {}
    for raw_id, raw in value.items():
        account_id = _text(raw_id, "account_id")
        if not isinstance(raw, Mapping):
            raise ValidationError("account must be a mapping", field="initial_accounts")
        currency = _text(raw.get("currency"), "currency").upper()
        balance = raw.get("balance")
        if not _CURRENCY.fullmatch(currency):
            raise ValidationError("currency must contain three letters", field="currency")
        if isinstance(balance, bool) or not isinstance(balance, int) or balance < 0:
            raise ValidationError("balance must be a non-negative integer", field="balance")
        result[account_id] = {"currency": currency, "balance": balance}
    return dict(sorted(result.items()))


def normalize_events(value) -> tuple[LedgerEvent, ...]:
    if not isinstance(value, list) or not value:
        raise ValidationError("payload must be a non-empty list", field="payload")
    events = []
    event_ids = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValidationError("event must be a mapping", field=f"events[{index}]")
        event_id = _text(raw.get("event_id"), "event_id")
        if event_id in event_ids:
            raise ValidationError("event IDs must be unique", field="event_id")
        event_ids.add(event_id)
        sequence = raw.get("sequence")
        delta = raw.get("delta")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ValidationError("sequence must be a positive integer", field="sequence")
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise ValidationError("delta must be a non-zero integer", field="delta")
        currency = _text(raw.get("currency"), "currency").upper()
        if not _CURRENCY.fullmatch(currency):
            raise ValidationError("currency must contain three letters", field="currency")
        events.append(LedgerEvent(
            event_id=event_id,
            transaction_id=_text(raw.get("transaction_id"), "transaction_id"),
            account_id=_text(raw.get("account_id"), "account_id"),
            partition=_text(raw.get("partition"), "partition"),
            sequence=sequence,
            delta=delta,
            currency=currency,
        ))
    return tuple(sorted(events, key=lambda event: (
        event.partition, event.sequence, event.event_id)))
