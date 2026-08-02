import pytest

from conftest import rows
from inventory_import_pipeline import DuplicateExternalId, build_application


def test_same_batch_duplicate_reports_every_index_and_is_atomic():
    app = build_application()
    with pytest.raises(DuplicateExternalId) as caught:
        app.api.import_batch(rows("x", "y", "x", "x"), idempotency_key="dup")
    assert caught.value.external_id == "x"
    assert caught.value.indexes == (0, 2, 3)
    assert app.repository.count() == 0 and app.dedupe.count() == 0


def test_existing_identifier_is_conflict_without_partial_insert():
    app = build_application()
    app.api.import_batch(rows("existing"), idempotency_key="one")
    before = app.repository.snapshot()
    with pytest.raises(DuplicateExternalId) as caught:
        app.api.import_batch(rows("new", "existing"), idempotency_key="two")
    assert caught.value.indexes == (1,)
    assert app.repository.snapshot() == before
