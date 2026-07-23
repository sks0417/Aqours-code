from __future__ import annotations

from copy import deepcopy


class CheckpointRepository:
    def __init__(self):
        self._latest = None

    def save(self, checkpoint: dict):
        self._latest = deepcopy(checkpoint)

    def latest(self):
        return deepcopy(self._latest)

    def replace_latest(self, checkpoint: dict):
        self._latest = deepcopy(checkpoint)
