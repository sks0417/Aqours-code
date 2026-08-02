import inspect

import billing_export as package
from billing_export.api import BillingExportAPI


def test_public_api_surface():
    assert {"BillingExportApplication", "build_application", "BillingExportError",
            "ExportValidationError", "UnsupportedExportFormat"} <= set(package.__all__)
    assert str(inspect.signature(package.build_application)) == "()"
    assert str(inspect.signature(BillingExportAPI.export)) == (
        "(self, records, *, format, schema_version='v2')")
