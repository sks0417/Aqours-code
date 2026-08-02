from copy import deepcopy

from .errors import StorageFormatError
from .models import Document
from .serialization import to_v2


class DocumentRepository:
    def __init__(self, records):
        self._records = deepcopy(list(records))

    def _decode(self, record):
        if record.get("version") == 2:
            fields = record.get("fields", {})
            return Document(str(record["id"]), str(fields["title"]),
                            str(fields["body"]), tuple(record.get("labels", ())))
        return Document(str(record["doc_id"]), str(record["title"]),
                        str(record["body"]), tuple(record.get("tags", ())))

    def documents(self):
        return tuple(self._decode(record) for record in self._records)

    def migrate_all(self):
        rewritten = 0
        migrated = []
        for record in self._records:
            # BUG: already-v2 records are rewritten and legacy tags are dropped.
            document = self._decode(record)
            document = Document(document.document_id, document.title, document.body, ())
            migrated.append(to_v2(document))
            rewritten += 1
        self._records = migrated
        return rewritten

    def raw_snapshot(self):
        return deepcopy(self._records)
