from .api import InventoryImportAPI
from .dedupe import ImportDedupeRepository
from .repository import InventoryRepository
from .service import InventoryImportService


class InventoryImportApplication:
    def __init__(self, api, repository, dedupe):
        self.api = api
        self.repository = repository
        self.dedupe = dedupe


def build_application():
    repository = InventoryRepository()
    dedupe = ImportDedupeRepository()
    service = InventoryImportService(repository, dedupe)
    return InventoryImportApplication(InventoryImportAPI(service), repository, dedupe)
