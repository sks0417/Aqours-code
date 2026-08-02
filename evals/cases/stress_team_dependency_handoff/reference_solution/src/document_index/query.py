from .serialization import serialize_result
from .validation import validate_query


class DocumentQueryService:
    def __init__(self, repository):
        self.repository = repository

    def search(self, query="", *, tag=None, page=1, page_size=20):
        needle, normalized_tag = validate_query(query, tag, page, page_size)
        documents = [document for document in self.repository.documents()
                     if needle in document.title.casefold()
                     or needle in document.body.casefold()]
        if normalized_tag is not None:
            documents = [document for document in documents
                         if normalized_tag in document.tags]
        documents.sort(key=lambda document: (document.title.casefold(),
                                             document.document_id))
        start = (page - 1) * page_size
        return [serialize_result(document)
                for document in documents[start:start + page_size]]
