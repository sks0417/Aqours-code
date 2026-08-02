from conftest import records
from document_index import build_application


def test_migration_is_lossless_canonical_and_idempotent():
    app = build_application(records())
    assert app.api.migrate_storage() == 2
    snapshot = app.repository.raw_snapshot()
    assert all(record.get("version") == 2 for record in snapshot)
    assert snapshot[0] == {"version": 2, "id": "b",
                           "fields": {"title": "beta", "body": "A long body about Python migration"},
                           "labels": ["guide", "python"]}
    assert app.api.migrate_storage() == 0
    assert app.repository.raw_snapshot() == snapshot


def test_mixed_storage_decodes_to_same_public_data():
    app = build_application(records())
    before = app.api.search()
    app.api.migrate_storage()
    assert app.api.search() == before
