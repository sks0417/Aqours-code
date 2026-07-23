from __future__ import annotations

from ..errors import IdempotencyConflict


class IdempotencyRepository:
    def __init__(self):
        self._bindings = {}

    def resolve(self, key: str, fingerprint: str):
        binding = self._bindings.get(key)
        if binding is None:
            return None
        stored_fingerprint, receipt = binding
        if stored_fingerprint != fingerprint:
            raise IdempotencyConflict(key)
        return receipt

    def bind(self, key: str, fingerprint: str, receipt):
        self._bindings[key] = (fingerprint, receipt)

    def snapshot(self):
        return dict(self._bindings)

    def restore(self, snapshot):
        self._bindings = dict(snapshot)
