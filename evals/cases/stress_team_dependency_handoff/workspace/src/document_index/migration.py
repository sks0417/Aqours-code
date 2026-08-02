class StorageMigrationService:
    def __init__(self, repository):
        self.repository = repository

    def migrate(self):
        return self.repository.migrate_all()
