import inspect

import inventory_import_pipeline as package
from inventory_import_pipeline.api import InventoryImportAPI


def test_public_surface_and_exception_attributes():
    assert {"InventoryImportApplication", "build_application", "ImportPipelineError",
            "ImportValidationError", "DuplicateExternalId", "IdempotencyConflict",
            "parse_csv", "parse_json"} <= set(package.__all__)
    assert str(inspect.signature(package.build_application)) == "()"
    assert str(inspect.signature(InventoryImportAPI.import_batch)) == (
        "(self, payload, *, idempotency_key)")
    duplicate = package.DuplicateExternalId("x", (1, 2))
    assert duplicate.external_id == "x" and duplicate.indexes == (1, 2)
    validation = package.ImportValidationError(("bad",))
    assert validation.errors == ("bad",)
