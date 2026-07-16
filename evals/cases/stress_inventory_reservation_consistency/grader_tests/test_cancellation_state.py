from __future__ import annotations

import pytest

from conftest import request
from inventory_service import (
    InvalidStateTransition,
    ReservationNotFound,
)


def test_repeated_cancel_is_a_no_op_and_returns_the_same_snapshot(make_application):
    app = make_application({"A": 8, "B": 5})
    created = app.api.reserve(
        request("order-cancel", ("A", 3), ("B", 2)),
        idempotency_key="cancel-once",
    )
    assert app.inventory.snapshot() == {"A": 5, "B": 3}

    first = app.api.cancel(created["reservation_id"])
    after_first = app.inventory.snapshot()
    second = app.api.cancel(created["reservation_id"])
    third = app.api.cancel(created["reservation_id"])

    assert first == second == third
    assert first["status"] == "canceled"
    assert after_first == {"A": 8, "B": 5}
    assert app.inventory.snapshot() == after_first
    assert app.reservations.count() == 1


def test_cancel_restores_only_the_target_reservations_exact_quantities(
    make_application,
):
    app = make_application({"A": 12, "B": 10, "C": 8})
    first = app.api.reserve(
        request("order-one", ("A", 4), ("B", 2)),
        idempotency_key="cancel:first",
    )
    second = app.api.reserve(
        request("order-two", ("A", 3), ("C", 5)),
        idempotency_key="cancel:second",
    )
    assert app.inventory.snapshot() == {"A": 5, "B": 8, "C": 3}

    app.api.cancel(first["reservation_id"])

    assert app.inventory.snapshot() == {"A": 9, "B": 10, "C": 3}
    assert app.api.get_reservation(second["reservation_id"])["status"] == "pending"


def test_confirming_a_canceled_reservation_is_rejected_without_stock_change(
    make_application,
):
    app = make_application({"A": 5})
    created = app.api.reserve(
        request("order-canceled", ("A", 2)),
        idempotency_key="state:canceled",
    )
    app.api.cancel(created["reservation_id"])
    before = app.inventory.snapshot()

    with pytest.raises(InvalidStateTransition) as caught:
        app.api.confirm(created["reservation_id"])

    assert caught.value.reservation_id == created["reservation_id"]
    assert caught.value.current == "canceled"
    assert caught.value.target == "confirmed"
    assert app.inventory.snapshot() == before
    assert app.api.get_reservation(created["reservation_id"])["status"] == "canceled"


def test_canceling_confirmed_reservation_is_rejected_without_releasing_stock(
    make_application,
):
    app = make_application({"A": 5})
    created = app.api.reserve(
        request("order-confirmed", ("A", 2)),
        idempotency_key="state:confirmed",
    )
    app.api.confirm(created["reservation_id"])
    before = app.inventory.snapshot()

    with pytest.raises(InvalidStateTransition) as caught:
        app.api.cancel(created["reservation_id"])

    assert caught.value.current == "confirmed"
    assert caught.value.target == "canceled"
    assert app.inventory.snapshot() == before
    assert app.api.get_reservation(created["reservation_id"])["status"] == "confirmed"


def test_operation_sequence_preserves_final_inventory_invariant(make_application):
    app = make_application({"A": 20, "B": 12, "C": 9})
    one = app.api.reserve(
        request("one", ("A", 4), ("B", 3)),
        idempotency_key="sequence:one",
    )
    two = app.api.reserve(
        request("two", ("A", 5), ("C", 4)),
        idempotency_key="sequence:two",
    )
    app.api.reserve(
        request("two", ("C", 4), ("A", 5)),
        idempotency_key="sequence:two",
    )
    app.api.cancel(one["reservation_id"])
    app.api.cancel(one["reservation_id"])
    app.api.confirm(two["reservation_id"])
    app.api.confirm(two["reservation_id"])

    assert app.inventory.snapshot() == {"A": 15, "B": 12, "C": 5}
    assert all(
        0 <= available <= app.inventory.initial(sku)
        for sku, available in app.inventory.snapshot().items()
    )
    assert [item.status.value for item in app.reservations.all()] == [
        "canceled",
        "confirmed",
    ]


def test_unknown_reservation_operations_leave_inventory_untouched(make_application):
    app = make_application({"A": 5})
    before = app.inventory.snapshot()

    with pytest.raises(ReservationNotFound):
        app.api.cancel("rsv-does-not-exist")
    with pytest.raises(ReservationNotFound):
        app.api.confirm("rsv-does-not-exist")

    assert app.inventory.snapshot() == before
