from .errors import ExportValidationError, UnsupportedExportFormat


class BillingExportAPI:
    def __init__(self, csv_service, json_service, normalizer):
        self._csv = csv_service
        self._json = json_service
        self._normalizer = normalizer

    def export(self, records, *, format, schema_version="v2"):
        if schema_version not in {"v1", "v2"}:
            raise ExportValidationError("unsupported schema version")
        normalized = self._normalizer(records)
        if format == "csv":
            return self._csv.export(normalized)
        if format == "json":
            return self._json.export(normalized)
        raise UnsupportedExportFormat(format)
