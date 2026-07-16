from __future__ import annotations

import pytest

from conftest import request
from inventory_service import ReservationStatus, ValidationError
from inventory_service.serialization import request_fingerprint
from inventory_service.validation import validate_reservation_request


def test_successful_reservations_remain_independent(make_application):
    app = make_application({"A": 10, "B": 6})
    first = app.api.reserve(
        request("regression-one", ("A", 2), ("B", 1)),
        idempotency_key="regression:one",
    )
    second = app.api.reserve(
        request("regression-two", ("A", 3)),
        idempotency_key="regression:two",
    )

    assert first["reservation_id"] != second["reservation_id"]
    assert app.inventory.snapshot() == {"A": 5, "B": 5}
    assert app.reservations.count() == 2
    assert app.idempotency.count() == 2


def test_normalized_fingerprint_is_stable_and_quantity_sensitive():
    split = validate_reservation_request({
        "order_id": " order-fingerprint ",
        "items": [
            {"sku": "B", "quantity": 1},
            {"sku": "A", "quantity": 2},
            {"sku": "B", "quantity": 2},
        ],
    })
    combined = validate_reservation_request(
        request("order-fingerprint", ("A", 2), ("B", 3))
    )
    different_quantity = validate_reservation_request(
        request("order-fingerprint", ("A", 3), ("B", 3))
    )

    assert request_fingerprint(split) == request_fingerprint(combined)
    assert request_fingerprint(combined) != request_fingerprint(different_quantity)


def test_status_values_and_serialized_values_remain_lowercase_strings():
    assert ReservationStatus.PENDING.value == "pending"
    assert ReservationStatus.CONFIRMED.value == "confirmed"
    assert ReservationStatus.CANCELED.value == "canceled"
    assert isinstance(ReservationStatus.PENDING, str)


@pytest.mark.parametrize(
    "bad_stock",
    [None, {}, {"": 1}, {"A": -1}, {"A": 1.5}, {"A": False}],
)
def test_initial_stock_validation_regression(bad_stock):
    from inventory_service import build_application

    with pytest.raises(ValidationError):
        build_application(bad_stock)


def test_unknown_payload_fields_remain_forward_compatible(make_application):
    app = make_application({"A": 4})
    body = request("forward-compatible", ("A", 1))
    body["future_field"] = {"ignored": True}
    body["items"][0]["future_line_field"] = "ignored"

    result = app.api.reserve(body, idempotency_key="regression:future")

    assert result["status"] == "pending"
    assert app.inventory.snapshot() == {"A": 3}
