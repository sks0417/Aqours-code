from __future__ import annotations

import pytest

from conftest import event, state
from ledger_service import CurrencyMismatch, InsufficientFunds, UnknownAccount


@pytest.mark.parametrize("tail,error", [
    (event("evt-2", "missing", 2, 1), UnknownAccount),
    (event("evt-2", "acct-b", 2, 1, currency="EUR"), CurrencyMismatch),
    (event("evt-2", "acct-b", 2, -99), InsufficientFunds),
])
def test_any_late_failure_rolls_back_every_repository(make_application, tail, error):
    app = make_application()
    before = state(app)
    with pytest.raises(error):
        app.api.ingest([
            event("evt-1", "acct-a", 1, -3), tail,
        ], idempotency_key="atomic:key")
    assert state(app) == before


def test_multiple_deltas_for_one_account_are_validated_as_a_batch(make_application):
    app = make_application()
    before = state(app)
    with pytest.raises(InsufficientFunds):
        app.api.ingest([
            event("evt-1", "acct-a", 1, 5),
            event("evt-2", "acct-a", 2, -20),
        ], idempotency_key="aggregate:funds")
    assert state(app) == before


def test_success_updates_cross_partition_state_once(make_application):
    app = make_application()
    receipt = app.api.ingest([
        event("evt-w1", "acct-b", 1, 2, partition="west"),
        event("evt-e1", "acct-a", 1, -2, partition="east"),
    ], idempotency_key="cross:ok")
    assert receipt["event_ids"] == ["evt-e1", "evt-w1"]
    assert receipt["balances"] == {"acct-a": 8, "acct-b": 7, "acct-eur": 7}
    assert receipt["sequences"] == {"east": 1, "west": 1}
