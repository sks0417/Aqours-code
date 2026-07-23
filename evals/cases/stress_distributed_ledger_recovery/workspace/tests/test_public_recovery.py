from __future__ import annotations

from conftest import event
from ledger_service import build_application


def test_recovery_restores_checkpoint_and_replays_durable_tail():
    app = build_application({
        "acct-a": {"currency": "USD", "balance": 10},
        "acct-b": {"currency": "USD", "balance": 0},
    })
    app.api.ingest([
        event("evt-1", "acct-a", 1, -2),
        event("evt-2", "acct-b", 2, 2),
    ], idempotency_key="batch:one")
    app.api.create_checkpoint()
    app.api.ingest([
        event("evt-3", "acct-a", 3, -1),
        event("evt-4", "acct-b", 4, 1),
    ], idempotency_key="batch:two")

    app.balances.restore({"acct-a": 999, "acct-b": 999})
    app.sequences.restore({"east": 999})
    recovered = app.api.recover()

    assert recovered == {
        "balances": {"acct-a": 7, "acct-b": 3},
        "sequences": {"east": 4},
        "event_count": 4,
    }
    assert app.api.get_balance("acct-a")["balance"] == 7
    assert app.api.get_partition_sequence("east") == 4
