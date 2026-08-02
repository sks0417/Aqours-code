class BillingExportError(Exception):
    pass


class ExportValidationError(BillingExportError):
    pass


class UnsupportedExportFormat(BillingExportError):
    pass
