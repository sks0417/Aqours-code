import pytest

from conftest import records
from document_index import QueryValidationError, build_application


@pytest.mark.parametrize("page,page_size", [(0, 1), (1, 0), (True, 1), (1, 1.5)])
def test_invalid_pagination_rejected(page, page_size):
    with pytest.raises(QueryValidationError):
        build_application(records()).api.search(page=page, page_size=page_size)


def test_tag_is_trimmed_and_exact():
    app = build_application(records())
    assert [item["document_id"] for item in app.api.search(tag=" python ")] == ["a", "b"]
