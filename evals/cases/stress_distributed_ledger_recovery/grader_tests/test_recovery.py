from __future__ import annotations

from copy import deepcopy

import pytest

from conftest import event, state
from ledger_service import CheckpointCorrupt


def _checkpointed_app(make_application):
    app = make_application()
    app.api.ingest([
        event("evt-1", "acct-a", 1, -2),
        event("evt-2", "acct-b", 2, 2),
    ], idempotency_key="one")
    app.api.create_checkpoint()
    app.api.ingest([
        event("evt-3", "acct-a", 3, -1),
        event("evt-4", "acct-b", 4, 1),
    ], idempotency_key="two")
    return app


@pytest.mark.parametrize("mutate", [
    lambda cp: cp["balances"].update({"acct-a": 999}),
    lambda cp: cp["sequences"].update({"east": 999}),
    lambda cp: cp.update({"event_count": 99}),
    lambda cp: cp["event_ids"].reverse(),
])
def test_corrupt_checkpoint_is_rejected_before_live_mutation(make_application, mutate):
    app = _checkpointed_app(make_application)
    checkpoint = app.checkpoints.latest()
    mutate(checkpoint)
    app.checkpoints.replace_latest(checkpoint)
    before = state(app)
    with pytest.raises(CheckpointCorrupt):
        app.api.recover()
    assert state(app) == before


def test_valid_checkpoint_replays_tail_without_touching_other_repositories(make_application):
    app = _checkpointed_app(make_application)
    immutable = {
        "events": app.event_store.snapshot(),
        "idempotency": app.idempotency.snapshot(),
        "receipts": app.receipts.snapshot(),
        "next_id": app.receipt_ids.snapshot(),
    }
    app.balances.restore({"acct-a": 0, "acct-b": 0, "acct-eur": 0})
    app.sequences.restore({})
    result = app.api.recover()
    assert result == {
        "balances": {"acct-a": 7, "acct-b": 8, "acct-eur": 7},
        "sequences": {"east": 4}, "event_count": 4,
    }
    assert app.event_store.snapshot() == immutable["events"]
    assert app.idempotency.snapshot() == immutable["idempotency"]
    assert app.receipts.snapshot() == immutable["receipts"]
    assert app.receipt_ids.snapshot() == immutable["next_id"]


def test_no_checkpoint_rebuilds_from_initial_state(make_application):
    app = make_application()
    app.api.ingest([event("evt-1", "acct-a", 1, -3)], idempotency_key="one")
    app.balances.restore({"acct-a": 100, "acct-b": 100, "acct-eur": 100})
    app.sequences.restore({"east": 100})
    result = app.api.recover()
    assert result["balances"] == {"acct-a": 7, "acct-b": 5, "acct-eur": 7}
    assert result["sequences"] == {"east": 1}
