from __future__ import annotations

import pytest

from conftest import event
from ledger_service import ValidationError, build_application


@pytest.mark.parametrize("value", [True, -1, "10"])
def test_initial_balance_requires_non_negative_integer(value):
    with pytest.raises(ValidationError):
        build_application({"acct-a": {"currency": "USD", "balance": value}})


def test_event_strings_trim_and_currency_normalizes():
    app = build_application({"acct-a": {"currency": "usd", "balance": 10}})
    payload = [event(
        " evt-1 ", " acct-a ", 1, 2,
        partition=" east ", transaction_id=" txn-1 ", currency=" usd ")]
    receipt = app.api.ingest(payload, idempotency_key=" key:one ")
    assert receipt["event_ids"] == ["evt-1"]
    assert app.api.get_balance("acct-a") == {
        "account_id": "acct-a", "currency": "USD", "balance": 12}


@pytest.mark.parametrize("field,value", [
    ("sequence", True), ("sequence", 0), ("delta", False), ("delta", 0),
])
def test_sequence_and_delta_reject_bool_and_zero(field, value):
    app = build_application({"acct-a": {"currency": "USD", "balance": 10}})
    row = event("evt-1", "acct-a", 1, 1)
    row[field] = value
    with pytest.raises(ValidationError) as caught:
        app.api.ingest([row], idempotency_key="key")
    assert caught.value.field == field
