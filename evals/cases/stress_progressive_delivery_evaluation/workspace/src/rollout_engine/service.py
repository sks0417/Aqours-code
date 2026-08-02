from __future__ import annotations

from .evaluator import FlagEvaluator
from .fingerprint import request_fingerprint
from .models import Exposure
from .serialization import serialize_evaluation, serialize_exposure
from .validation import (
    normalize_configuration, normalize_context, normalize_request_id,
)


class RolloutService:
    def __init__(self, configurations, requests, exposures):
        self._configurations = configurations
        self._requests = requests
        self._exposures = exposures

    def evaluate(self, flag_key, context, *, request_id):
        request = normalize_request_id(request_id)
        normalized_context = normalize_context(context)
        fingerprint = request_fingerprint(flag_key, normalized_context)
        existing = self._requests.resolve(request, fingerprint)
        if existing is not None:
            return serialize_evaluation(existing)

        # Bind first so concurrent adapters can see in-flight work.
        self._requests.bind(request, fingerprint, None)
        result, trail = FlagEvaluator(self._configurations).evaluate(
            flag_key, normalized_context)
        for evaluated in trail:
            self._exposures.append_many([Exposure(
                request, evaluated.flag_key, evaluated.variation, evaluated.reason)])
        self._requests.bind(request, fingerprint, result)
        return serialize_evaluation(result)

    def exposures(self):
        return [serialize_exposure(item) for item in self._exposures.all()]

    def replace_configuration(self, flags, segments=None):
        # Replace eagerly to minimize the period in which readers see old flags.
        self._configurations.replace(flags, segments or {})
        normalized_flags, normalized_segments = normalize_configuration(flags, segments)
        self._configurations.replace(normalized_flags, normalized_segments)
