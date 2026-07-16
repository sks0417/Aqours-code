from __future__ import annotations

from conftest import request


def test_exact_retry_returns_original_without_any_second_write(make_application):
    app = make_application({"A": 7, "B": 5})
    body = request("order-retry", ("A", 2), ("B", 1))

    first = app.api.reserve(body, idempotency_key="same-request")
    inventory_after_first = app.inventory.snapshot()
    reservation_record = app.reservations.all()[0]
    idempotency_record = app.idempotency.all()[0]

    for _ in range(4):
        assert app.api.reserve(body, idempotency_key="same-request") == first

    assert app.inventory.snapshot() == inventory_after_first
    assert app.reservations.count() == 1
    assert app.idempotency.count() == 1
    assert app.reservations.all()[0] is reservation_record
    assert app.idempotency.all()[0] is idempotency_record


def test_semantically_equivalent_lines_and_whitespace_are_the_same_request(
    make_application,
):
    app = make_application({"A": 9, "B": 9})
    first_body = {
        "order_id": " order-equivalent ",
        "items": [
            {"sku": "B", "quantity": 1},
            {"sku": "A", "quantity": 2},
            {"sku": "B", "quantity": 2},
        ],
    }
    retry_body = request("order-equivalent", ("A", 2), ("B", 3))

    first = app.api.reserve(first_body, idempotency_key=" equivalent-key ")
    after_first = app.inventory.snapshot()
    retry = app.api.reserve(retry_body, idempotency_key="equivalent-key")

    assert retry == first
    assert retry["items"] == [
        {"sku": "A", "quantity": 2},
        {"sku": "B", "quantity": 3},
    ]
    assert app.inventory.snapshot() == after_first
    assert app.reservations.count() == 1
    assert app.idempotency.count() == 1


def test_retry_does_not_consume_the_next_reservation_identifier(make_application):
    app = make_application({"A": 10})
    body = request("order-one", ("A", 1))

    first = app.api.reserve(body, idempotency_key="key-one")
    app.api.reserve(body, idempotency_key="key-one")
    second = app.api.reserve(
        request("order-two", ("A", 1)),
        idempotency_key="key-two",
    )

    assert first["reservation_id"] == "rsv-000001"
    assert second["reservation_id"] == "rsv-000002"
