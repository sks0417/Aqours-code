from __future__ import annotations

import pytest

from conftest import event
from ledger_service import (
    CurrencyMismatch, DuplicateEvent, IdempotencyConflict, InsufficientFunds,
    SequenceConflict, UnknownAccount, ValidationError,
)


@pytest.mark.parametrize("name", [
    "ValidationError", "UnknownAccount", "CurrencyMismatch",
    "InsufficientFunds", "SequenceConflict", "DuplicateEvent",
    "IdempotencyConflict",
])
def test_documented_errors_inherit_base(name):
    import ledger_service

    assert issubclass(getattr(ledger_service, name), ledger_service.LedgerServiceError)


def test_receipt_and_query_shapes_remain_stable(make_application):
    app = make_application()
    receipt = app.api.ingest([
        event("west-1", "acct-b", 1, 2, partition="west"),
        event("east-1", "acct-a", 1, -2, partition="east"),
    ], idempotency_key="shape")
    assert list(receipt) == ["batch_id", "event_ids", "balances", "sequences"]
    assert list(receipt["balances"]) == sorted(receipt["balances"])
    assert list(receipt["sequences"]) == sorted(receipt["sequences"])
    assert app.api.get_balance("acct-a") == {
        "account_id": "acct-a", "currency": "USD", "balance": 8}


def test_unknown_account_and_currency_expose_fields(make_application):
    app = make_application()
    with pytest.raises(UnknownAccount) as unknown:
        app.api.ingest([event("evt-1", "missing", 1, 1)], idempotency_key="u")
    assert unknown.value.account_id == "missing"
    with pytest.raises(CurrencyMismatch) as currency:
        app.api.ingest([
            event("evt-2", "acct-a", 1, 1, currency="EUR")],
            idempotency_key="c")
    assert (currency.value.account_id, currency.value.expected, currency.value.actual) == (
        "acct-a", "USD", "EUR")
