class ImportPipelineError(Exception):
    pass


class ImportValidationError(ImportPipelineError):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class DuplicateExternalId(ImportPipelineError):
    def __init__(self, external_id, indexes):
        self.external_id = external_id
        self.indexes = tuple(indexes)
        super().__init__(f"duplicate external_id {external_id} at {self.indexes}")


class IdempotencyConflict(ImportPipelineError):
    pass
