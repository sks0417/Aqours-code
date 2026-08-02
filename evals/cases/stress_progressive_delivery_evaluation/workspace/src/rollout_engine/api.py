from __future__ import annotations


class RolloutAPI:
    def __init__(self, service):
        self._service = service

    def evaluate(self, flag_key, context, *, request_id):
        return self._service.evaluate(flag_key, context, request_id=request_id)

    def exposures(self):
        return self._service.exposures()

    def replace_configuration(self, flags, segments=None):
        return self._service.replace_configuration(flags, segments)
