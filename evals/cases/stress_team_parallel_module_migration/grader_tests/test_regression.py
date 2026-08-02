import pytest

from billing_export import ExportValidationError, UnsupportedExportFormat, build_application


def test_conflicting_aliases_rejected_before_encoding():
    row = {"id": "old", "invoice_id": "new", "customer": "c",
           "amount_cents": 1, "currency": "USD"}
    with pytest.raises(ExportValidationError):
        build_application().api.export([row], format="json")


def test_format_and_version_remain_strict():
    app = build_application()
    with pytest.raises(UnsupportedExportFormat):
        app.api.export([], format="JSON")
    with pytest.raises(ExportValidationError):
        app.api.export([], format="json", schema_version="v3")
