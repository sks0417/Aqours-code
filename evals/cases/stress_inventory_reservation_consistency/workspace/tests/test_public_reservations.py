from __future__ import annotations

import pytest

from inventory_service import (
    InsufficientInventory,
    InvalidStateTransition,
    ReservationNotFound,
    build_application,
)


def payload(order_id="order-100", *items):
    if not items:
        items = (
            {"sku": "A-100", "quantity": 2},
            {"sku": "B-200", "quantity": 1},
        )
    return {"order_id": order_id, "items": list(items)}


def test_normal_multi_item_reservation_uses_documented_response_shape():
    app = build_application({"A-100": 8, "B-200": 5})

    result = app.api.reserve(payload(), idempotency_key="checkout:100")

    assert result == {
        "reservation_id": "rsv-000001",
        "order_id": "order-100",
        "status": "pending",
        "items": [
            {"sku": "A-100", "quantity": 2},
            {"sku": "B-200", "quantity": 1},
        ],
    }
    assert app.api.get_inventory("A-100") == {
        "sku": "A-100",
        "available": 6,
        "initial": 8,
    }
    assert app.api.get_inventory("B-200")["available"] == 4
    assert app.reservations.count() == 1
    assert app.idempotency.count() == 1


def test_failed_batch_leaves_all_inventory_and_repositories_unchanged():
    app = build_application({"A-100": 8, "B-200": 0, "C-300": 4})
    before = app.inventory.snapshot()

    with pytest.raises(InsufficientInventory) as caught:
        app.api.reserve(
            payload(
                "order-atomic",
                {"sku": "A-100", "quantity": 3},
                {"sku": "B-200", "quantity": 1},
                {"sku": "C-300", "quantity": 2},
            ),
            idempotency_key="checkout:atomic",
        )

    assert caught.value.sku == "B-200"
    assert app.inventory.snapshot() == before
    assert app.reservations.count() == 0
    assert app.idempotency.count() == 0


def test_same_key_and_same_request_returns_original_reservation():
    app = build_application({"A-100": 8, "B-200": 5})
    request = payload()

    first = app.api.reserve(request, idempotency_key="checkout:retry")
    after_first = app.inventory.snapshot()
    second = app.api.reserve(request, idempotency_key="checkout:retry")

    assert second == first
    assert app.inventory.snapshot() == after_first
    assert app.reservations.count() == 1
    assert app.idempotency.count() == 1


def test_cancel_restores_reserved_quantities_once():
    app = build_application({"A-100": 8, "B-200": 5})
    reservation = app.api.reserve(payload(), idempotency_key="checkout:cancel")

    canceled = app.api.cancel(reservation["reservation_id"])

    assert canceled["status"] == "canceled"
    assert app.inventory.snapshot() == {"A-100": 8, "B-200": 5}
    assert app.api.get_reservation(reservation["reservation_id"]) == canceled


def test_confirm_is_idempotent_and_confirmed_reservation_cannot_be_canceled():
    app = build_application({"A-100": 3})
    created = app.api.reserve(
        payload("order-confirm", {"sku": "A-100", "quantity": 2}),
        idempotency_key="checkout:confirm",
    )

    confirmed = app.api.confirm(created["reservation_id"])
    confirmed_again = app.api.confirm(created["reservation_id"])

    assert confirmed_again == confirmed
    assert confirmed["status"] == "confirmed"
    with pytest.raises(InvalidStateTransition):
        app.api.cancel(created["reservation_id"])
    assert app.api.get_inventory("A-100")["available"] == 1


def test_missing_reservation_keeps_domain_exception_semantics():
    app = build_application({"A-100": 1})

    with pytest.raises(ReservationNotFound) as caught:
        app.api.get_reservation("rsv-missing")

    assert caught.value.reservation_id == "rsv-missing"
