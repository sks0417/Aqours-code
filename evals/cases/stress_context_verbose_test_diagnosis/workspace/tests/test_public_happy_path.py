from inventory_import_pipeline import build_application


def test_ordinary_batch_import():
    app = build_application()
    result = app.api.import_batch([
        {"external_id": "source-1", "sku": "A", "quantity": 2},
        {"external_id": "source-2", "sku": "B", "quantity": 3, "future": True},
    ], idempotency_key="batch:ordinary")
    assert result == {
        "batch_id": "batch-0001", "imported_count": 2,
        "external_ids": ["source-1", "source-2"],
    }
    assert app.repository.count() == 2
