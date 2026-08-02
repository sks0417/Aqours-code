from copy import deepcopy

from .models import Document
from .serialization import to_v2


def _unique(values):
    return tuple(dict.fromkeys(str(value).strip() for value in values))


class DocumentRepository:
    def __init__(self, records):
        self._records = deepcopy(list(records))

    def _decode(self, record):
        if record.get("version") == 2:
            fields = record.get("fields", {})
            return Document(str(record["id"]), str(fields["title"]),
                            str(fields["body"]), _unique(record.get("labels", ())))
        return Document(str(record["doc_id"]), str(record["title"]),
                        str(record["body"]), _unique(record.get("tags", ())))

    def documents(self):
        return tuple(self._decode(record) for record in self._records)

    def migrate_all(self):
        rewritten = 0
        migrated = []
        for record in self._records:
            document = self._decode(record)
            canonical = to_v2(document)
            migrated.append(canonical)
            if record != canonical:
                rewritten += 1
        self._records = migrated
        return rewritten

    def raw_snapshot(self):
        return deepcopy(self._records)
