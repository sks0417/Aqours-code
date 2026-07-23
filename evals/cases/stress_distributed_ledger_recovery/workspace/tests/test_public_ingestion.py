from __future__ import annotations

import pytest

from conftest import event
from ledger_service import InsufficientFunds, SequenceConflict, build_application


def accounts():
    return {
        "acct-a": {"currency": "USD", "balance": 10},
        "acct-b": {"currency": "USD", "balance": 0},
    }


def test_failed_cross_account_batch_is_atomic():
    app = build_application(accounts())
    before = app.balances.snapshot()
    with pytest.raises(InsufficientFunds):
        app.api.ingest([
            event("evt-1", "acct-a", 1, -5),
            event("evt-2", "acct-b", 2, -1),
        ], idempotency_key="batch:atomic")

    assert app.balances.snapshot() == before
    assert app.event_store.all() == ()
    assert app.sequences.snapshot() == {}
    assert app.idempotency.snapshot() == {}
    assert app.receipt_ids.snapshot() == 1


def test_success_and_retry_apply_exactly_once():
    app = build_application(accounts())
    payload = [
        event("evt-1", "acct-a", 1, -3),
        event("evt-2", "acct-b", 2, 3),
    ]
    first = app.api.ingest(payload, idempotency_key="batch:one")
    second = app.api.ingest(list(reversed(payload)), idempotency_key="batch:one")

    assert first == second
    assert first["batch_id"] == "batch-000001"
    assert first["event_ids"] == ["evt-1", "evt-2"]
    assert first["balances"] == {"acct-a": 7, "acct-b": 3}
    assert len(app.event_store.all()) == 2
    assert app.receipt_ids.snapshot() == 2


def test_partition_gap_rejects_the_complete_batch():
    app = build_application(accounts())
    with pytest.raises(SequenceConflict) as caught:
        app.api.ingest([
            event("evt-1", "acct-a", 1, 1),
            event("evt-3", "acct-a", 3, 1),
        ], idempotency_key="batch:gap")
    assert (caught.value.partition, caught.value.expected, caught.value.actual) == (
        "east", 2, 3)
    assert app.event_store.all() == ()
    assert app.balances.snapshot() == {"acct-a": 10, "acct-b": 0}
