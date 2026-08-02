from conftest import rows
from inventory_import_pipeline import build_application


def test_independent_successful_batches_remain_queryable():
    app = build_application()
    first = app.api.import_batch(rows("a", "b"), idempotency_key="one")
    second = app.api.import_batch(rows("c"), idempotency_key="two")
    assert first["batch_id"] == "batch-0001"
    assert second["batch_id"] == "batch-0002"
    assert app.repository.count() == 3


def test_input_order_is_preserved():
    app = build_application()
    result = app.api.import_batch(rows("z", "a", "m"), idempotency_key="order")
    assert result["external_ids"] == ["z", "a", "m"]
