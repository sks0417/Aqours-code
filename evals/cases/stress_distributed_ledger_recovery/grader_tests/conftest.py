from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


def event(event_id, account_id, sequence, delta, *, partition="east",
          transaction_id="txn-1", currency="USD"):
    return {
        "event_id": event_id, "transaction_id": transaction_id,
        "account_id": account_id, "partition": partition,
        "sequence": sequence, "delta": delta, "currency": currency,
    }


@pytest.fixture
def make_application():
    from ledger_service import build_application

    def factory(accounts=None):
        return build_application(accounts or {
            "acct-a": {"currency": "USD", "balance": 10},
            "acct-b": {"currency": "USD", "balance": 5},
            "acct-eur": {"currency": "EUR", "balance": 7},
        })
    return factory


def state(app):
    return {
        "events": app.event_store.snapshot(),
        "balances": app.balances.snapshot(),
        "sequences": app.sequences.snapshot(),
        "idempotency": app.idempotency.snapshot(),
        "receipts": app.receipts.snapshot(),
        "next_id": app.receipt_ids.snapshot(),
    }
