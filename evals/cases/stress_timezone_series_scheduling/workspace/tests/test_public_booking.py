from __future__ import annotations

import pytest

from conftest import daily, resources
from series_scheduler import CapacityExceeded, build_application


def test_booking_retry_is_exactly_once_and_capacity_is_cumulative():
    app = build_application(resources(capacity=3))
    app.api.create_series(daily(count=1), idempotency_key="series")
    start = "2026-03-07T14:00Z"
    first = app.api.book("clinic", start, 2, request_id="book:one")
    retry = app.api.book("clinic", start, 2, request_id=" book:one ")
    assert first == retry
    with pytest.raises(CapacityExceeded) as caught:
        app.api.book("clinic", start, 2, request_id="book:two")
    assert (caught.value.requested, caught.value.available) == (2, 1)
    assert len(app.api.bookings()) == 1
    assert app.booking_ids.snapshot() == 2


def test_cancel_hides_occurrence():
    app = build_application(resources())
    app.api.create_series(daily(count=1), idempotency_key="series")
    app.api.cancel_occurrence("clinic", "2026-03-07T14:00Z")
    assert app.api.occurrences(
        "clinic", "2026-03-07T00:00Z", "2026-03-08T00:00Z") == []
