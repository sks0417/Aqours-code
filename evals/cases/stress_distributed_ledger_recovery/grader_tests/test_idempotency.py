from __future__ import annotations

import pytest

from conftest import event, state
from ledger_service import DuplicateEvent, IdempotencyConflict, InsufficientFunds


def test_delta_is_part_of_idempotency_fingerprint(make_application):
    app = make_application()
    app.api.ingest([event("evt-1", "acct-a", 1, -2)], idempotency_key="same:key")
    before = state(app)
    with pytest.raises(IdempotencyConflict) as caught:
        app.api.ingest([event("evt-1", "acct-a", 1, -3)], idempotency_key="same:key")
    assert caught.value.key == "same:key"
    assert state(app) == before


def test_every_normalized_field_changes_the_fingerprint(make_application):
    base = event("evt-1", "acct-a", 1, -1)
    mutations = {
        "event_id": "evt-other", "transaction_id": "txn-other",
        "account_id": "acct-b", "partition": "west", "sequence": 2,
        "delta": -2, "currency": "EUR",
    }
    for field, value in mutations.items():
        app = make_application()
        app.api.ingest([base], idempotency_key="fingerprint")
        changed = dict(base)
        changed[field] = value
        with pytest.raises(IdempotencyConflict):
            app.api.ingest([changed], idempotency_key="fingerprint")


def test_failed_attempt_binds_nothing_and_consumes_no_id(make_application):
    app = make_application()
    payload = [event("evt-1", "acct-a", 1, -20)]
    with pytest.raises(InsufficientFunds):
        app.api.ingest(payload, idempotency_key="retry:later")
    assert app.idempotency.snapshot() == {}
    assert app.receipt_ids.snapshot() == 1

    app.balances.restore({"acct-a": 30, "acct-b": 5, "acct-eur": 7})
    receipt = app.api.ingest(payload, idempotency_key="retry:later")
    assert receipt["batch_id"] == "batch-000001"


def test_duplicate_event_under_another_key_is_side_effect_free(make_application):
    app = make_application()
    app.api.ingest([event("evt-1", "acct-a", 1, 1)], idempotency_key="key:one")
    before = state(app)
    with pytest.raises(DuplicateEvent) as caught:
        app.api.ingest([event("evt-1", "acct-a", 2, 1)], idempotency_key="key:two")
    assert caught.value.event_id == "evt-1"
    assert state(app) == before
