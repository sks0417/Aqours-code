from __future__ import annotations

import inspect

from conftest import event


def test_exports_and_signatures_are_stable(make_application):
    import ledger_service

    expected = {
        "LedgerApplication", "build_api", "build_application",
        "LedgerServiceError", "ValidationError", "UnknownAccount",
        "CurrencyMismatch", "InsufficientFunds", "SequenceConflict",
        "DuplicateEvent", "IdempotencyConflict", "CheckpointCorrupt",
    }
    assert expected <= set(ledger_service.__all__)
    app = make_application()
    assert str(inspect.signature(app.api.ingest)) == "(payload, *, idempotency_key)"
    assert str(inspect.signature(app.api.get_balance)) == "(account_id)"
    assert str(inspect.signature(app.api.get_partition_sequence)) == "(partition)"
    assert str(inspect.signature(app.api.create_checkpoint)) == "()"
    assert str(inspect.signature(app.api.recover)) == "()"


def test_return_values_are_fresh_json_compatible_copies(make_application):
    app = make_application()
    receipt = app.api.ingest(
        [event("evt-1", "acct-a", 1, 1)], idempotency_key="copy")
    receipt["balances"]["acct-a"] = 999
    receipt["event_ids"].append("forged")
    retried = app.api.ingest(
        [event("evt-1", "acct-a", 1, 1)], idempotency_key="copy")
    assert retried["balances"]["acct-a"] == 11
    assert retried["event_ids"] == ["evt-1"]

    checkpoint = app.api.create_checkpoint()
    checkpoint["balances"]["acct-a"] = 999
    assert app.checkpoints.latest()["balances"]["acct-a"] == 11


def test_facade_does_not_reach_into_repository_storage():
    import ledger_service.api as api_module

    source = inspect.getsource(api_module.LedgerAPI)
    assert "._events" not in source
    assert "._balances" not in source
    assert "._bindings" not in source
    assert "._values" not in source
