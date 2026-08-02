from collections import deque

from .errors import ProviderUnavailable


class ScriptedProvider:
    def __init__(self, *outcomes):
        self.outcomes = deque(outcomes)
        self.calls = []

    def deliver(self, request):
        self.calls.append(request)
        if not self.outcomes:
            raise ProviderUnavailable("no scripted outcome")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(request)
        return outcome


class ProviderRegistry:
    def __init__(self, providers):
        self._providers = dict(providers)

    def get(self, channel):
        return self._providers.get(channel)
