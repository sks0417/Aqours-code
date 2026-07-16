from __future__ import annotations

import pytest

from conftest import request
from inventory_service import InsufficientInventory, UnknownSku


@pytest.mark.parametrize(
    ("stock", "lines", "failed_sku"),
    [
        ({"A": 0, "B": 5, "C": 5}, (("A", 1), ("B", 2), ("C", 2)), "A"),
        ({"A": 5, "B": 0, "C": 5}, (("A", 2), ("B", 1), ("C", 2)), "B"),
        ({"A": 5, "B": 5, "C": 0}, (("A", 2), ("B", 2), ("C", 1)), "C"),
    ],
)
def test_insufficient_line_at_any_position_rolls_back_every_repository(
    make_application,
    stock,
    lines,
    failed_sku,
):
    app = make_application(stock)
    before = app.inventory.snapshot()

    with pytest.raises(InsufficientInventory) as caught:
        app.api.reserve(
            request("order-atomic", *lines),
            idempotency_key=f"atomic:{failed_sku}",
        )

    assert caught.value.sku == failed_sku
    assert app.inventory.snapshot() == before
    assert app.reservations.count() == 0
    assert app.idempotency.count() == 0


@pytest.mark.parametrize("missing_sku", ["AA", "BB", "ZZ"])
def test_unknown_sku_never_leaves_earlier_deductions(make_application, missing_sku):
    app = make_application({"A": 5, "B": 5, "C": 5})
    lines = (("A", 2), (missing_sku, 1), ("C", 2))
    before = app.inventory.snapshot()

    with pytest.raises(UnknownSku) as caught:
        app.api.reserve(
            request(f"order-{missing_sku}", *lines),
            idempotency_key=f"unknown:{missing_sku}",
        )

    assert caught.value.sku == missing_sku
    assert app.inventory.snapshot() == before
    assert app.reservations.count() == 0
    assert app.idempotency.count() == 0


def test_failed_key_is_not_bound_and_can_be_retried_after_a_new_application_state(
    make_application,
):
    failed = make_application({"A": 0, "B": 2})
    body = request("order-retry-after-failure", ("A", 1), ("B", 1))

    with pytest.raises(InsufficientInventory):
        failed.api.reserve(body, idempotency_key="not-bound")

    assert failed.idempotency.count() == 0
    assert failed.reservations.count() == 0
    assert failed.inventory.snapshot() == {"A": 0, "B": 2}

    sufficient = make_application({"A": 2, "B": 2})
    result = sufficient.api.reserve(body, idempotency_key="not-bound")
    assert result["status"] == "pending"
    assert sufficient.inventory.snapshot() == {"A": 1, "B": 1}
