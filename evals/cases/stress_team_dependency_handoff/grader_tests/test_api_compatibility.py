import inspect

import document_index as package
from document_index.api import DocumentIndexAPI


def test_public_surface_and_signatures():
    assert {"DocumentIndexApplication", "build_application", "DocumentIndexError",
            "QueryValidationError", "StorageFormatError"} <= set(package.__all__)
    assert str(inspect.signature(package.build_application)) == "(initial_records)"
    assert str(inspect.signature(DocumentIndexAPI.migrate_storage)) == "(self)"
    assert str(inspect.signature(DocumentIndexAPI.search)) == (
        "(self, query='', *, tag=None, page=1, page_size=20)")
