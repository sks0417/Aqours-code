from __future__ import annotations

import pytest

from conftest import event, state
from ledger_service import SequenceConflict


def test_gap_inside_one_batch_reports_first_missing_sequence(make_application):
    app = make_application()
    with pytest.raises(SequenceConflict) as caught:
        app.api.ingest([
            event("evt-1", "acct-a", 1, 1),
            event("evt-3", "acct-a", 3, 1),
        ], idempotency_key="gap")
    assert (caught.value.expected, caught.value.actual) == (2, 3)


def test_one_bad_partition_rolls_back_other_partition(make_application):
    app = make_application()
    app.api.ingest([
        event("east-1", "acct-a", 1, 1, partition="east"),
        event("west-1", "acct-b", 1, 1, partition="west"),
    ], idempotency_key="first")
    before = state(app)
    with pytest.raises(SequenceConflict):
        app.api.ingest([
            event("east-2", "acct-a", 2, 1, partition="east"),
            event("west-3", "acct-b", 3, 1, partition="west"),
        ], idempotency_key="bad-west")
    assert state(app) == before


def test_stale_sequence_is_rejected_without_changes(make_application):
    app = make_application()
    app.api.ingest([event("evt-1", "acct-a", 1, 1)], idempotency_key="first")
    before = state(app)
    with pytest.raises(SequenceConflict) as caught:
        app.api.ingest([event("evt-2", "acct-a", 1, 1)], idempotency_key="stale")
    assert (caught.value.expected, caught.value.actual) == (2, 1)
    assert state(app) == before
