from __future__ import annotations

from ..errors import DuplicateEvent


class EventStore:
    def __init__(self):
        self._events = []
        self._ids = set()

    def append_many(self, events):
        for event in events:
            if event.event_id in self._ids:
                raise DuplicateEvent(event.event_id)
        self._events.extend(events)
        self._ids.update(event.event_id for event in events)

    def all(self):
        return tuple(self._events)

    def snapshot(self):
        return list(self._events), set(self._ids)

    def restore(self, snapshot):
        events, ids = snapshot
        self._events, self._ids = list(events), set(ids)
