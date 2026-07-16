from __future__ import annotations

import pytest

from inventory_service import UnknownSku, ValidationError, build_application


@pytest.mark.parametrize(
    ("input_payload", "field"),
    [
        ({"order_id": "", "items": [{"sku": "A", "quantity": 1}]}, "order_id"),
        ({"order_id": "o-1", "items": []}, "items"),
        ({"order_id": "o-1", "items": [{"sku": "", "quantity": 1}]}, "items[0].sku"),
        ({"order_id": "o-1", "items": [{"sku": "A", "quantity": 0}]}, "items[0].quantity"),
        ({"order_id": "o-1", "items": [{"sku": "A", "quantity": True}]}, "items[0].quantity"),
    ],
)
def test_invalid_reservation_input_reports_the_documented_field(input_payload, field):
    app = build_application({"A": 5})

    with pytest.raises(ValidationError) as caught:
        app.api.reserve(input_payload, idempotency_key="valid-key")

    assert caught.value.field == field
    assert app.inventory.snapshot() == {"A": 5}


@pytest.mark.parametrize("key", ["", "spaces are invalid", "x" * 129])
def test_invalid_idempotency_keys_are_rejected_before_inventory_changes(key):
    app = build_application({"A": 5})

    with pytest.raises(ValidationError) as caught:
        app.api.reserve(
            {"order_id": "o-1", "items": [{"sku": "A", "quantity": 1}]},
            idempotency_key=key,
        )

    assert caught.value.field == "idempotency_key"
    assert app.inventory.snapshot() == {"A": 5}


def test_duplicate_lines_are_combined_and_sorted_in_the_public_response():
    app = build_application({"A": 5, "B": 5})

    result = app.api.reserve(
        {
            "order_id": "  o-1  ",
            "items": [
                {"sku": "B", "quantity": 1},
                {"sku": "A", "quantity": 2},
                {"sku": "B", "quantity": 2},
            ],
        },
        idempotency_key="normalized:1",
    )

    assert result["order_id"] == "o-1"
    assert result["items"] == [
        {"sku": "A", "quantity": 2},
        {"sku": "B", "quantity": 3},
    ]
    assert app.inventory.snapshot() == {"A": 3, "B": 2}


def test_unknown_sku_is_not_translated_to_validation_error():
    app = build_application({"A": 5})

    with pytest.raises(UnknownSku) as caught:
        app.api.reserve(
            {"order_id": "o-1", "items": [{"sku": "MISSING", "quantity": 1}]},
            idempotency_key="unknown:1",
        )

    assert caught.value.sku == "MISSING"
    assert app.inventory.snapshot() == {"A": 5}


def test_initial_stock_validation_rejects_boolean_and_negative_quantities():
    with pytest.raises(ValidationError):
        build_application({"A": True})
    with pytest.raises(ValidationError):
        build_application({"A": -1})
