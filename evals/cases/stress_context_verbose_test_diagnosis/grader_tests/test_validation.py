import pytest

from inventory_import_pipeline import ImportValidationError, build_application


def test_validation_aggregates_in_row_order_without_mutation():
    app = build_application()
    with pytest.raises(ImportValidationError) as caught:
        app.api.import_batch([
            {"external_id": "", "sku": "A", "quantity": 0},
            {"external_id": "b", "sku": "", "quantity": True},
        ], idempotency_key="invalid")
    assert caught.value.errors == (
        "row 0: external_id must be a non-empty string",
        "row 0: quantity must be a positive integer",
        "row 1: sku must be a non-empty string",
        "row 1: quantity must be a positive integer",
    )
    assert app.repository.count() == 0 and app.dedupe.count() == 0


def test_unknown_fields_do_not_affect_idempotency():
    app = build_application()
    first = [{"external_id": "x", "sku": "A", "quantity": 1, "future": 1}]
    second = [{"external_id": "x", "sku": "A", "quantity": 1, "future": 2}]
    assert app.api.import_batch(first, idempotency_key="future") == app.api.import_batch(
        second, idempotency_key="future")
