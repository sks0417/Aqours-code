from .api import BillingExportAPI
from .common import normalize_records
from .csv_export import CSVExportService, CSVInvoiceEncoder
from .json_export import JSONExportService, JSONInvoiceEncoder


class BillingExportApplication:
    def __init__(self, api):
        self.api = api


def build_application():
    api = BillingExportAPI(
        CSVExportService(CSVInvoiceEncoder()),
        JSONExportService(JSONInvoiceEncoder()), normalize_records)
    return BillingExportApplication(api)
