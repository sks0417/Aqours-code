from __future__ import annotations


class ReceiptIdSequence:
    def __init__(self):
        self._next = 1

    def allocate(self) -> str:
        value = f"batch-{self._next:06d}"
        self._next += 1
        return value

    def snapshot(self):
        return self._next

    def restore(self, snapshot):
        self._next = int(snapshot)


class ReceiptRepository:
    def __init__(self):
        self._values = {}

    def add(self, receipt):
        self._values[receipt.batch_id] = receipt

    def snapshot(self):
        return dict(self._values)

    def restore(self, snapshot):
        self._values = dict(snapshot)
