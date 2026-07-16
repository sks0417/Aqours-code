from __future__ import annotations

import pytest

from conftest import request
from inventory_service import IdempotencyConflict


@pytest.mark.parametrize(
    "conflicting_body",
    [
        request("order-original", ("A", 3), ("B", 1)),
        request("order-different", ("A", 2), ("B", 1)),
        request("order-original", ("A", 2), ("C", 1)),
    ],
    ids=["different-quantity", "different-order", "different-sku"],
)
def test_same_key_with_different_normalized_payload_is_a_conflict(
    make_application,
    conflicting_body,
):
    app = make_application({"A": 10, "B": 10, "C": 10})
    original = request("order-original", ("A", 2), ("B", 1))
    created = app.api.reserve(original, idempotency_key="conflict-key")
    before = app.inventory.snapshot()

    with pytest.raises(IdempotencyConflict) as caught:
        app.api.reserve(conflicting_body, idempotency_key="conflict-key")

    assert caught.value.key == "conflict-key"
    assert app.inventory.snapshot() == before
    assert app.reservations.count() == 1
    assert app.idempotency.count() == 1
    assert app.api.get_reservation(created["reservation_id"]) == created


def test_conflict_does_not_hide_the_original_result(make_application):
    app = make_application({"A": 6})
    original = request("order-stable", ("A", 2))
    first = app.api.reserve(original, idempotency_key="stable-key")

    with pytest.raises(IdempotencyConflict):
        app.api.reserve(
            request("order-stable", ("A", 3)),
            idempotency_key="stable-key",
        )

    assert app.api.reserve(original, idempotency_key="stable-key") == first
    assert app.inventory.snapshot() == {"A": 4}
