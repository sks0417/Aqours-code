from conftest import records
from document_index import build_application


def test_searches_title_and_body_after_migration_with_stable_sort():
    app = build_application(records())
    app.api.migrate_storage()
    assert [item["document_id"] for item in app.api.search("python")] == ["a", "b"]
    assert [item["document_id"] for item in app.api.search()] == ["a", "c", "b"]


def test_filter_precedes_pagination():
    app = build_application(records())
    app.api.migrate_storage()
    first = app.api.search(tag="python", page=1, page_size=1)
    second = app.api.search(tag="python", page=2, page_size=1)
    assert [item["document_id"] for item in first] == ["a"]
    assert [item["document_id"] for item in second] == ["b"]


def test_result_shape_and_snippet_are_detached():
    app = build_application(records())
    item = app.api.search("migration")[0]
    assert set(item) == {"document_id", "title", "snippet", "tags"}
    assert item["snippet"] == "A long body about Python migration"[:40]
    item["tags"].append("changed")
    assert "changed" not in app.api.search("migration")[0]["tags"]
