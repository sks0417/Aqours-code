class DocumentIndexAPI:
    def __init__(self, migration, query):
        self._migration = migration
        self._query = query

    def migrate_storage(self):
        return self._migration.migrate()

    def search(self, query="", *, tag=None, page=1, page_size=20):
        return self._query.search(query, tag=tag, page=page, page_size=page_size)
