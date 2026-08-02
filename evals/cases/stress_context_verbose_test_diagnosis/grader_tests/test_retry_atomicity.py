import pytest

from conftest import rows
from inventory_import_pipeline import IdempotencyConflict, build_application


def test_retry_is_detached_and_does_not_write_again():
    app = build_application()
    first = app.api.import_batch(rows("a", "b"), idempotency_key="retry")
    first["external_ids"].append("mutated")
    again = app.api.import_batch(rows("a", "b"), idempotency_key="retry")
    assert again == {"batch_id": "batch-0001", "imported_count": 2,
                     "external_ids": ["a", "b"]}
    assert app.repository.count() == 2 and app.dedupe.count() == 1


def test_reused_key_with_changed_batch_conflicts_before_mutation():
    app = build_application()
    app.api.import_batch(rows("a"), idempotency_key="key")
    before = app.repository.snapshot()
    with pytest.raises(IdempotencyConflict):
        app.api.import_batch(rows("b"), idempotency_key="key")
    assert app.repository.snapshot() == before


def test_batch_sequence_advances_only_after_success():
    app = build_application()
    app.api.import_batch(rows("a"), idempotency_key="one")
    result = app.api.import_batch(rows("b"), idempotency_key="two")
    assert result["batch_id"] == "batch-0002"
