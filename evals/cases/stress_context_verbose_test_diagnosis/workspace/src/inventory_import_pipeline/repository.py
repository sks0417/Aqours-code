from copy import deepcopy

from .errors import DuplicateExternalId


class InventoryRepository:
    def __init__(self):
        self._rows = {}

    def insert_many(self, rows):
        for index, row in enumerate(rows):
            if row.external_id in self._rows:
                raise DuplicateExternalId(row.external_id, (index,))
        for row in rows:
            self._rows[row.external_id] = row

    def snapshot(self):
        return deepcopy(self._rows)

    def count(self):
        return len(self._rows)
