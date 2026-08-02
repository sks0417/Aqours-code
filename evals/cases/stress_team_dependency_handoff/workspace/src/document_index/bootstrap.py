from .api import DocumentIndexAPI
from .migration import StorageMigrationService
from .query import DocumentQueryService
from .storage import DocumentRepository


class DocumentIndexApplication:
    def __init__(self, api, repository):
        self.api = api
        self.repository = repository


def build_application(initial_records):
    repository = DocumentRepository(initial_records)
    api = DocumentIndexAPI(StorageMigrationService(repository),
                           DocumentQueryService(repository))
    return DocumentIndexApplication(api, repository)
