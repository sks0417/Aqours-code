class DocumentIndexError(Exception):
    pass


class StorageFormatError(DocumentIndexError):
    pass


class QueryValidationError(DocumentIndexError):
    pass
