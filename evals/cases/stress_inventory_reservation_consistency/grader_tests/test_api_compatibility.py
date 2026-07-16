from __future__ import annotations

import inspect

import pytest

import inventory_service
from conftest import request
from inventory_service import (
    IdempotencyConflict,
    InsufficientInventory,
    InventoryServiceError,
    InvalidStateTransition,
    ReservationAPI,
    ReservationNotFound,
    UnknownSku,
    ValidationError,
    build_application,
)


def parameter_shape(callable_object):
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(callable_object).parameters.values()
    ]


def test_facade_method_signatures_remain_compatible():
    reserve = parameter_shape(ReservationAPI.reserve)
    assert [entry[0] for entry in reserve] == ["self", "payload", "idempotency_key"]
    assert reserve[2][1] is inspect.Parameter.KEYWORD_ONLY
    assert reserve[2][2] is inspect.Parameter.empty

    for method_name, argument in [
        ("cancel", "reservation_id"),
        ("confirm", "reservation_id"),
        ("get_reservation", "reservation_id"),
        ("get_inventory", "sku"),
    ]:
        shape = parameter_shape(getattr(ReservationAPI, method_name))
        assert [entry[0] for entry in shape] == ["self", argument]
        assert shape[1][1] is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_documented_symbols_and_error_hierarchy_are_exported():
    required = {
        "ReservationAPI",
        "ReservationService",
        "InventoryRepository",
        "ReservationRepository",
        "IdempotencyRepository",
        "ReservationStatus",
        "InventoryServiceError",
        "ValidationError",
        "UnknownSku",
        "InsufficientInventory",
        "ReservationNotFound",
        "IdempotencyConflict",
        "InvalidStateTransition",
        "InventoryInvariantViolation",
        "build_api",
        "build_application",
    }
    assert required <= set(inventory_service.__all__)
    assert all(hasattr(inventory_service, name) for name in required)
    for error_type in (
        ValidationError,
        UnknownSku,
        InsufficientInventory,
        ReservationNotFound,
        IdempotencyConflict,
        InvalidStateTransition,
    ):
        assert issubclass(error_type, InventoryServiceError)


def test_domain_exception_attributes_are_preserved(make_application):
    app = make_application({"A": 1})
    with pytest.raises(InsufficientInventory) as insufficient:
        app.api.reserve(
            request("too-many", ("A", 2)),
            idempotency_key="error:stock",
        )
    assert (
        insufficient.value.sku,
        insufficient.value.requested,
        insufficient.value.available,
    ) == ("A", 2, 1)

    with pytest.raises(UnknownSku) as unknown:
        app.api.get_inventory("MISSING")
    assert unknown.value.sku == "MISSING"

    with pytest.raises(ValidationError) as invalid:
        app.api.reserve({}, idempotency_key="error:validation")
    assert invalid.value.field == "order_id"


def test_facade_returns_detached_serialized_values(make_application):
    app = make_application({"A": 5})
    created = app.api.reserve(
        request("detached", ("A", 2)),
        idempotency_key="api:detached",
    )
    created["status"] = "corrupted"
    created["items"][0]["quantity"] = 1000

    stored = app.api.get_reservation("rsv-000001")
    assert stored["status"] == "pending"
    assert stored["items"] == [{"sku": "A", "quantity": 2}]
    assert app.inventory.snapshot() == {"A": 3}


def test_application_keeps_repository_boundaries_available(make_application):
    app = make_application({"A": 5})
    app.api.reserve(
        request("repository-boundary", ("A", 1)),
        idempotency_key="api:repository",
    )

    assert app.service.inventory is app.inventory
    assert app.service.reservations is app.reservations
    assert app.service.idempotency is app.idempotency
    assert app.reservations.count() == 1
    assert app.idempotency.count() == 1
