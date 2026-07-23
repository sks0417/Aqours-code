from __future__ import annotations

from collections import defaultdict

from ..errors import SequenceConflict


class SequenceRepository:
    def __init__(self):
        self._values = {}

    def validate(self, events):
        grouped = defaultdict(list)
        for event in events:
            grouped[event.partition].append(event.sequence)
        for partition, sequences in grouped.items():
            expected = self._values.get(partition, 0) + 1
            if sequences[0] != expected:
                raise SequenceConflict(partition, expected, sequences[0])

    def advance(self, events):
        for event in events:
            self._values[event.partition] = event.sequence

    def get(self, partition: str) -> int:
        return self._values.get(partition, 0)

    def snapshot(self):
        return dict(self._values)

    def restore(self, snapshot):
        self._values = dict(snapshot)
