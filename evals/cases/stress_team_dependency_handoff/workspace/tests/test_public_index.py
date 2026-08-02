from document_index import build_application


def records():
    return [{"doc_id": "one", "title": "Alpha", "body": "Searchable body",
             "tags": ["guide"]},
            {"version": 2, "id": "two", "fields": {"title": "Beta", "body": "Other"},
             "labels": ["reference"]}]


def test_reads_both_storage_versions():
    result = build_application(records()).api.search("Alpha")
    assert result[0]["document_id"] == "one"


def test_migration_then_search_preserves_tags():
    app = build_application(records())
    assert app.api.migrate_storage() == 1
    assert app.api.search(tag="guide")[0]["tags"] == ["guide"]
